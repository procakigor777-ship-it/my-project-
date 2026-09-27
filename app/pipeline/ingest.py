"""Сбор отзывов из магазинов.

Идемпотентен по (store, store_review_id): повторный прогон ничего не
дублирует. Если пользователь отредактировал отзыв, меняется хеш тела —
анализ и неопубликованный черновик сбрасываются, и отзыв проходит
конвейер заново.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Reply, ReplyStatus, Review, Store, as_utc, utcnow
from app.stores.base import FetchedReview, StoreClient, StoreError

logger = logging.getLogger(__name__)

#: Небольшой перехлёст по времени, чтобы не терять отзывы, пришедшие
#: в ту же минуту, что и предыдущий прогон.
OVERLAP = timedelta(minutes=10)


@dataclass
class IngestStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "IngestStats") -> None:
        self.created += other.created
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.errors.extend(other.errors)


def last_seen(session: Session, store: Store) -> datetime | None:
    stmt = select(Review.created_at).where(Review.store == store).order_by(Review.created_at.desc())
    newest = as_utc(session.execute(stmt.limit(1)).scalar_one_or_none())
    return newest - OVERLAP if newest else None


def ingest(session: Session, clients: dict[Store, StoreClient]) -> IngestStats:
    total = IngestStats()
    for store, client in clients.items():
        try:
            fetched = client.fetch_reviews(since=last_seen(session, store))
        except StoreError as exc:
            logger.error("ingest %s: %s", store.value, exc)
            total.errors.append(f"{store.value}: {exc}")
            continue
        total.merge(_persist(session, fetched))
    session.flush()
    return total


def _persist(session: Session, fetched: list[FetchedReview]) -> IngestStats:
    stats = IngestStats()
    for item in fetched:
        existing = session.execute(
            select(Review).where(
                Review.store == item.store, Review.store_review_id == item.store_review_id
            )
        ).scalar_one_or_none()

        if existing is None:
            session.add(_to_model(item))
            stats.created += 1
            continue

        if existing.body_hash == item.body_hash and existing.rating == item.rating:
            stats.unchanged += 1
            continue

        _apply_update(session, existing, item)
        stats.updated += 1
    return stats


def _to_model(item: FetchedReview) -> Review:
    return Review(
        store=item.store,
        store_review_id=item.store_review_id,
        rating=item.rating,
        title=item.title,
        body=item.body,
        body_hash=item.body_hash,
        author=item.author,
        locale=item.locale,
        territory=item.territory,
        app_version=item.app_version,
        device=item.device,
        created_at=item.created_at,
        raw=item.raw,
    )


def _apply_update(session: Session, review: Review, item: FetchedReview) -> None:
    """Отзыв изменился: обновляем текст и сбрасываем всё, что от него зависело."""
    review.rating = item.rating
    review.title = item.title
    review.body = item.body
    review.body_hash = item.body_hash
    review.app_version = item.app_version or review.app_version
    review.raw = item.raw
    review.updated_at = utcnow()

    if review.analysis is not None:
        session.delete(review.analysis)
        review.analysis = None
    for topic in list(review.topics):
        session.delete(topic)
    review.topics = []

    # Опубликованный ответ не трогаем — он уже висит в магазине.
    # Неопубликованные черновики к новому тексту не относятся.
    for reply in review.replies:
        if reply.status in (ReplyStatus.PENDING, ReplyStatus.NEEDS_HUMAN, ReplyStatus.APPROVED):
            reply.status = ReplyStatus.OBSOLETE
            reply.last_error = "Отзыв отредактирован пользователем — черновик неактуален"


def stale_ingest_hours(session: Session, store: Store) -> float | None:
    """Сколько часов назад мы в последний раз что-то видели из этого магазина.

    Для Google Play это критично: окно доступности отзывов в API — около
    недели, всё, что старше, не вернуть уже никогда.
    """
    newest = session.execute(
        select(Review.fetched_at)
        .where(Review.store == store)
        .order_by(Review.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if newest is None:
        return None
    return (utcnow() - as_utc(newest)).total_seconds() / 3600
