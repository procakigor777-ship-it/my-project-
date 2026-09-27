"""Правила алертов.

Намеренно обычный SQL, а не LLM: алерт должен быть детерминированным,
дешёвым и объяснимым. Модель подключается уровнем выше — в недельной
сводке, где нужна группировка по смыслу.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Review, ReviewAnalysis, ReviewTopic, Store, utcnow
from app.pipeline.ingest import stale_ingest_hours

#: Ниже этого числа отзывов за сутки доли слишком шумные, чтобы по ним
#: что-то объявлять.
MIN_SAMPLE = 5

#: Во сколько раз доля негатива за сутки должна превысить месячную базу.
NEGATIVE_SPIKE_FACTOR = 1.8

#: Google Play отдаёт только последние ~7 дней. Предупреждаем сильно заранее.
PLAY_STALE_WARN_HOURS = 24
PLAY_STALE_CRITICAL_HOURS = 96


@dataclass
class Alert:
    level: str  # info | warning | critical
    title: str
    detail: str


def _share(session: Session, since_hours: float) -> tuple[int, float]:
    cutoff = utcnow() - timedelta(hours=since_hours)
    total, negative = session.execute(
        select(
            func.count(Review.id),
            func.sum(case((Review.rating <= 2, 1), else_=0)),
        ).where(Review.created_at >= cutoff)
    ).one()
    total = total or 0
    negative = negative or 0
    return total, (negative / total if total else 0.0)


def check_negative_spike(session: Session) -> list[Alert]:
    day_total, day_share = _share(session, 24)
    if day_total < MIN_SAMPLE:
        return []
    _, base_share = _share(session, 24 * 30)
    if base_share == 0:
        return []
    if day_share >= base_share * NEGATIVE_SPIKE_FACTOR:
        return [
            Alert(
                level="critical",
                title="Всплеск негатива за сутки",
                detail=(
                    f"Доля 1-2★ за 24 часа: {day_share:.0%} при месячной базе "
                    f"{base_share:.0%} (отзывов за сутки: {day_total})"
                ),
            )
        ]
    return []


def check_topic_version_cluster(session: Session, hours: int = 24, threshold: int = 3) -> list[Alert]:
    """Несколько отзывов с одной темой на одной версии за короткое окно.

    Самый надёжный индикатор регрессии после релиза.
    """
    cutoff = utcnow() - timedelta(hours=hours)
    rows = session.execute(
        select(ReviewTopic.topic, Review.app_version, func.count(Review.id))
        .join(Review, Review.id == ReviewTopic.review_id)
        .where(
            Review.created_at >= cutoff,
            Review.app_version.is_not(None),
            Review.rating <= 3,
        )
        .group_by(ReviewTopic.topic, Review.app_version)
        .having(func.count(Review.id) >= threshold)
        .order_by(func.count(Review.id).desc())
    ).all()

    return [
        Alert(
            level="critical" if topic == "crash" else "warning",
            title=f"Кластер «{topic}» на версии {version}",
            detail=f"{count} отзывов с оценкой ≤3 за последние {hours} ч",
        )
        for topic, version, count in rows
    ]


def check_risk_flags(session: Session, hours: int = 24) -> list[Alert]:
    cutoff = utcnow() - timedelta(hours=hours)
    rows = list(
        session.execute(
            select(ReviewAnalysis)
            .join(Review, Review.id == ReviewAnalysis.review_id)
            .where(Review.created_at >= cutoff)
        ).scalars()
    )
    flagged = [a for a in rows if a.risk_flags]
    if not flagged:
        return []
    return [
        Alert(
            level="critical",
            title="Отзывы с юридическим или репутационным риском",
            detail=(
                f"{len(flagged)} шт. за {hours} ч: "
                + ", ".join(sorted({f for a in flagged for f in a.risk_flags}))
            ),
        )
    ]


def check_ingest_freshness(session: Session) -> list[Alert]:
    """Молчащий ingest для Google Play означает безвозвратную потерю отзывов."""
    out: list[Alert] = []
    hours = stale_ingest_hours(session, Store.GOOGLE_PLAY)
    if hours is None:
        return out
    if hours >= PLAY_STALE_CRITICAL_HOURS:
        out.append(
            Alert(
                level="critical",
                title="Google Play: сбор не работает",
                detail=(
                    f"Последний успешный сбор {hours:.0f} ч назад. Окно API — около 7 дней, "
                    "отзывы старше этого срока будут потеряны безвозвратно."
                ),
            )
        )
    elif hours >= PLAY_STALE_WARN_HOURS:
        out.append(
            Alert(
                level="warning",
                title="Google Play: давно не было новых отзывов",
                detail=f"Последний сбор {hours:.0f} ч назад — проверьте воркер и доступы.",
            )
        )
    return out


def run_all(session: Session) -> list[Alert]:
    alerts: list[Alert] = []
    alerts += check_risk_flags(session)
    alerts += check_negative_spike(session)
    alerts += check_topic_version_cluster(session)
    alerts += check_ingest_freshness(session)
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(alerts, key=lambda a: order.get(a.level, 3))
