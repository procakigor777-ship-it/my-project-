"""Агрегаты по отзывам — то, ради чего в основном всё и строится.

Ответы на отзывы это гигиена; польза — в том, что поток негатива
превращается в измеримые показатели по версиям, темам и локалям.
Считается обычным SQL, без участия модели.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import (
    Reply,
    ReplyStatus,
    Review,
    ReviewAnalysis,
    ReviewTopic,
    Sentiment,
    Store,
    utcnow,
)


def _cutoff(days: int):
    return utcnow() - timedelta(days=days)


@dataclass
class Totals:
    reviews: int
    negative: int
    average_rating: float

    @property
    def negative_share(self) -> float:
        return self.negative / self.reviews if self.reviews else 0.0


def totals(session: Session, days: int = 7, store: Store | None = None) -> Totals:
    stmt = select(
        func.count(Review.id),
        func.sum(case((Review.rating <= 2, 1), else_=0)),
        func.avg(Review.rating),
    ).where(Review.created_at >= _cutoff(days))
    if store:
        stmt = stmt.where(Review.store == store)
    count, negative, avg = session.execute(stmt).one()
    return Totals(reviews=count or 0, negative=negative or 0, average_rating=float(avg or 0))


def topics_breakdown(
    session: Session, days: int = 7, negative_only: bool = True, limit: int = 15
) -> list[tuple[str, int]]:
    stmt = (
        select(ReviewTopic.topic, func.count(ReviewTopic.id))
        .join(Review, Review.id == ReviewTopic.review_id)
        .where(Review.created_at >= _cutoff(days))
        .group_by(ReviewTopic.topic)
        .order_by(func.count(ReviewTopic.id).desc())
        .limit(limit)
    )
    if negative_only:
        stmt = stmt.join(ReviewAnalysis, ReviewAnalysis.review_id == Review.id).where(
            ReviewAnalysis.sentiment == Sentiment.NEGATIVE
        )
    return [(topic, count) for topic, count in session.execute(stmt)]


def by_version(session: Session, days: int = 14, limit: int = 10) -> list[dict]:
    """Разрез по версии приложения.

    Скачок негатива сразу после релиза — самый ценный сигнал во всей
    системе, поэтому разрез вынесен отдельно.
    """
    stmt = (
        select(
            Review.app_version,
            func.count(Review.id),
            func.sum(case((Review.rating <= 2, 1), else_=0)),
            func.avg(Review.rating),
        )
        .where(Review.created_at >= _cutoff(days), Review.app_version.is_not(None))
        .group_by(Review.app_version)
        .order_by(func.count(Review.id).desc())
        .limit(limit)
    )
    out = []
    for version, count, negative, avg in session.execute(stmt):
        out.append(
            {
                "app_version": version,
                "reviews": count,
                "negative": negative or 0,
                "negative_share": round((negative or 0) / count, 3) if count else 0.0,
                "average_rating": round(float(avg or 0), 2),
            }
        )
    return out


def by_locale(session: Session, days: int = 30, limit: int = 10) -> list[dict]:
    stmt = (
        select(
            Review.locale,
            func.count(Review.id),
            func.sum(case((Review.rating <= 2, 1), else_=0)),
        )
        .where(Review.created_at >= _cutoff(days))
        .group_by(Review.locale)
        .order_by(func.count(Review.id).desc())
        .limit(limit)
    )
    return [
        {"locale": locale or "—", "reviews": count, "negative": negative or 0}
        for locale, count, negative in session.execute(stmt)
    ]


def queue_stats(session: Session) -> dict[str, int]:
    stmt = select(Reply.status, func.count(Reply.id)).group_by(Reply.status)
    counts = {status.value: 0 for status in ReplyStatus}
    for status, count in session.execute(stmt):
        counts[status.value if hasattr(status, "value") else str(status)] = count
    return counts


def moderation_quality(session: Session, days: int = 30) -> dict:
    """Доля черновиков, ушедших в публикацию без правок.

    Главная метрика качества генерации: падает — значит промпт
    разошёлся с реальностью, и правки модераторов показывают, куда именно.
    """
    stmt = select(Reply).where(
        Reply.moderated_at.is_not(None), Reply.moderated_at >= _cutoff(days)
    )
    replies = list(session.execute(stmt).scalars())
    moderated = len(replies)
    if not moderated:
        return {"moderated": 0, "approved_clean": 0, "edited": 0, "rejected": 0, "clean_rate": 0.0}

    rejected = sum(1 for r in replies if r.status is ReplyStatus.REJECTED)
    edited = sum(1 for r in replies if r.was_edited)
    clean = moderated - rejected - edited
    return {
        "moderated": moderated,
        "approved_clean": clean,
        "edited": edited,
        "rejected": rejected,
        "clean_rate": round(clean / moderated, 3),
    }


def reject_reasons(session: Session, days: int = 30, limit: int = 10) -> list[tuple[str, int]]:
    """Топ причин отклонения — рабочий вход для правки промпта."""
    stmt = (
        select(Reply.reject_reason, func.count(Reply.id))
        .where(
            Reply.status == ReplyStatus.REJECTED,
            Reply.reject_reason.is_not(None),
            Reply.moderated_at >= _cutoff(days),
        )
        .group_by(Reply.reject_reason)
        .order_by(func.count(Reply.id).desc())
        .limit(limit)
    )
    return [(reason, count) for reason, count in session.execute(stmt)]


def dashboard(session: Session) -> dict:
    week = totals(session, days=7)
    prev = totals(session, days=14)
    prev_only_count = prev.reviews - week.reviews
    prev_only_negative = prev.negative - week.negative
    prev_share = prev_only_negative / prev_only_count if prev_only_count else 0.0

    return {
        "week": {
            "reviews": week.reviews,
            "negative": week.negative,
            "negative_share": round(week.negative_share, 3),
            "average_rating": round(week.average_rating, 2),
        },
        "previous_week_negative_share": round(prev_share, 3),
        "delta_negative_share": round(week.negative_share - prev_share, 3),
        "topics": topics_breakdown(session, days=7),
        "versions": by_version(session, days=14),
        "locales": by_locale(session, days=30),
        "queue": queue_stats(session),
        "moderation": moderation_quality(session),
    }
