"""Шаг публикации.

Публикуется только то, что подтвердил человек. Публикатор идемпотентен:
перед отправкой проверяет, что ответ ещё не опубликован, и фиксирует
идентификатор ответа магазина сразу после успеха.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Reply, ReplyStatus, Store, utcnow
from app.stores.base import ReviewGone, StoreClient, StoreError

logger = logging.getLogger(__name__)

#: После стольких неудачных попыток ответ возвращается модератору
#: вместо бесконечного ретрая.
MAX_ATTEMPTS = 5


@dataclass
class PublishStats:
    published: int = 0
    failed: int = 0
    retry_later: int = 0
    gone: int = 0
    errors: list[str] = field(default_factory=list)


def approved_replies(session: Session, limit: int = 100) -> list[Reply]:
    stmt = (
        select(Reply)
        .where(Reply.status == ReplyStatus.APPROVED, Reply.published_at.is_(None))
        .order_by(Reply.moderated_at)
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def publish(
    session: Session,
    clients: dict[Store, StoreClient],
    enabled: bool,
    limit: int = 100,
) -> PublishStats:
    stats = PublishStats()
    if not enabled:
        logger.warning("Публикация выключена (PUBLISH_ENABLED=false) — ничего не отправлено")
        return stats

    for reply in approved_replies(session, limit):
        review = reply.review
        client = clients.get(review.store)
        if client is None:
            stats.errors.append(f"reply {reply.id}: нет адаптера для {review.store.value}")
            continue

        text = reply.text_to_publish
        if not text:
            reply.status = ReplyStatus.FAILED
            reply.last_error = "Пустой текст ответа"
            stats.failed += 1
            continue

        reply.attempts += 1
        try:
            response_id = client.publish_reply(review.store_review_id, text)
        except ReviewGone as exc:
            # Отзыв удалили между модерацией и публикацией. Это норма, а не инцидент.
            reply.status = ReplyStatus.OBSOLETE
            reply.last_error = str(exc)
            stats.gone += 1
            continue
        except StoreError as exc:
            reply.last_error = str(exc)
            if exc.retryable and reply.attempts < MAX_ATTEMPTS:
                stats.retry_later += 1
                logger.warning("publish reply=%s, попытка %s: %s", reply.id, reply.attempts, exc)
            else:
                reply.status = ReplyStatus.FAILED
                stats.failed += 1
                stats.errors.append(f"reply {reply.id}: {exc}")
            continue

        reply.status = ReplyStatus.PUBLISHED
        reply.published_at = utcnow()
        reply.store_response_id = response_id
        reply.last_error = None
        stats.published += 1

    session.flush()
    return stats
