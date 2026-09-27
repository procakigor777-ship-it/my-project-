"""Действия модератора.

Вся ценность системы — в том, как быстро человек проходит очередь,
поэтому здесь минимум состояний: подтвердить, поправить, отклонить.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Reply, ReplyStatus, Review, ReviewAnalysis, utcnow

#: Очередь сортируется по severity, а не по времени: свежая мелочь
#: не должна закрывать собой вчерашнюю потерю денег.
QUEUE_STATUSES = (ReplyStatus.PENDING, ReplyStatus.NEEDS_HUMAN, ReplyStatus.FAILED)


class ModerationError(Exception):
    pass


@dataclass
class QueueItem:
    reply: Reply
    review: Review
    analysis: ReviewAnalysis | None


def queue(session: Session, limit: int = 50, offset: int = 0) -> list[QueueItem]:
    stmt = (
        select(Reply, Review, ReviewAnalysis)
        .join(Review, Review.id == Reply.review_id)
        .outerjoin(ReviewAnalysis, ReviewAnalysis.review_id == Review.id)
        .where(Reply.status.in_(QUEUE_STATUSES))
        .order_by(
            ReviewAnalysis.severity.desc().nulls_last(),
            Review.created_at.desc(),
        )
        .offset(offset)
        .limit(limit)
    )
    return [QueueItem(reply=r, review=rev, analysis=a) for r, rev, a in session.execute(stmt).all()]


def _load(session: Session, reply_id: int) -> Reply:
    reply = session.get(Reply, reply_id)
    if reply is None:
        raise ModerationError(f"Ответ {reply_id} не найден")
    if reply.status in (ReplyStatus.PUBLISHED, ReplyStatus.OBSOLETE):
        raise ModerationError(f"Ответ {reply_id} уже в статусе {reply.status.value}")
    return reply


def approve(session: Session, reply_id: int, moderator: str, text: str | None = None) -> Reply:
    """Подтверждает ответ, опционально с правками.

    Правленый текст сохраняется отдельно от черновика: дифф между ними —
    источник примеров для промпта и метрика качества генерации.
    """
    reply = _load(session, reply_id)
    final = (text if text is not None else reply.draft_text) or ""
    final = final.strip()
    if not final:
        raise ModerationError("Пустой текст ответа")
    if reply.char_limit and len(final) > reply.char_limit:
        raise ModerationError(
            f"Ответ длиннее лимита магазина: {len(final)} > {reply.char_limit} символов"
        )

    reply.final_text = final
    reply.status = ReplyStatus.APPROVED
    reply.moderator = moderator
    reply.moderated_at = utcnow()
    reply.last_error = None
    reply.attempts = 0
    session.flush()
    return reply


def reject(session: Session, reply_id: int, moderator: str, reason: str) -> Reply:
    """Отклоняет черновик.

    Причина обязательна: топ причин отклонения — главный рабочий вход
    для правки промпта.
    """
    if not reason.strip():
        raise ModerationError("Нужна причина отклонения — без неё промпт не починить")
    reply = _load(session, reply_id)
    reply.status = ReplyStatus.REJECTED
    reply.moderator = moderator
    reply.reject_reason = reason.strip()
    reply.moderated_at = utcnow()
    session.flush()
    return reply


def good_examples(session: Session, limit: int = 20) -> list[dict]:
    """Опубликованные ответы, которые модератор правил вручную.

    Готовый материал для few-shot примеров в промпте: это ответы,
    которые человек счёл достаточно хорошими для публикации.
    """
    stmt = (
        select(Reply, Review)
        .join(Review, Review.id == Reply.review_id)
        .where(Reply.status == ReplyStatus.PUBLISHED, Reply.final_text.is_not(None))
        .order_by(Reply.published_at.desc())
        .limit(limit)
    )
    return [
        {
            "review": f"{rev.title or ''}\n{rev.body}".strip(),
            "reply": reply.final_text,
            "was_edited": reply.was_edited,
        }
        for reply, rev in session.execute(stmt).all()
    ]
