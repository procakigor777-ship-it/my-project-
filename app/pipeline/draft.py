"""Шаг генерации черновиков.

Черновик создаётся не для всех отзывов: severity выше порога, любой
risk_flag или пустой текст уводят карточку сразу к человеку. Модель,
которая бойко отвечает на юридическую угрозу, — источник проблем,
а не экономии.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.brand import BrandProfile
from app.llm.client import LLMClient, LLMError
from app.llm.drafting import draft_reply
from app.llm.prompts import DRAFT_PROMPT_VERSION
from app.models import Reply, ReplyStatus, Review, ReviewAnalysis, Store
from app.stores.base import StoreClient

logger = logging.getLogger(__name__)

LIVE_STATUSES = (
    ReplyStatus.PENDING,
    ReplyStatus.NEEDS_HUMAN,
    ReplyStatus.APPROVED,
    ReplyStatus.PUBLISHED,
    ReplyStatus.REJECTED,
    ReplyStatus.FAILED,
)


@dataclass
class DraftStats:
    drafted: int = 0
    escalated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def awaiting_draft(session: Session, limit: int = 100) -> list[Review]:
    """Отзывы с готовым анализом, у которых нет ни одного живого ответа."""
    existing = select(Reply.review_id).where(Reply.status.in_(LIVE_STATUSES))
    stmt = (
        select(Review)
        .join(ReviewAnalysis, ReviewAnalysis.review_id == Review.id)
        .where(Review.id.not_in(existing))
        .order_by(ReviewAnalysis.severity.desc(), Review.created_at.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().unique())


def needs_human(analysis: ReviewAnalysis, ceiling: int) -> str | None:
    """Причина, по которой черновик генерировать нельзя, либо None."""
    if analysis.risk_flags:
        return f"risk_flags: {', '.join(analysis.risk_flags)}"
    if analysis.severity >= ceiling:
        return f"severity={analysis.severity} — нужен человек"
    return None


def generate_drafts(
    session: Session,
    llm: LLMClient,
    brand: BrandProfile,
    model: str,
    clients: dict[Store, StoreClient],
    severity_ceiling: int = 4,
    limit: int = 100,
) -> DraftStats:
    stats = DraftStats()
    for review in awaiting_draft(session, limit):
        analysis = review.analysis
        if analysis is None:
            continue

        client = clients.get(review.store)
        if client is None:
            stats.skipped += 1
            continue

        reason = needs_human(analysis, severity_ceiling)
        if reason:
            session.add(
                Reply(
                    review_id=review.id,
                    status=ReplyStatus.NEEDS_HUMAN,
                    last_error=reason,
                    char_limit=client.reply_char_limit,
                )
            )
            stats.escalated += 1
            continue

        try:
            result = draft_reply(
                llm, brand, model, review, analysis, client.reply_char_limit
            )
        except LLMError as exc:
            logger.error("draft review=%s: %s", review.id, exc)
            stats.errors.append(f"review {review.id}: {exc}")
            continue

        status = ReplyStatus.NEEDS_HUMAN if result.escalate_to_human else ReplyStatus.PENDING
        session.add(
            Reply(
                review_id=review.id,
                draft_text=result.reply_text,
                status=status,
                model=model,
                prompt_version=DRAFT_PROMPT_VERSION,
                char_limit=client.reply_char_limit,
                last_error=result.escalation_reason,
            )
        )
        if status is ReplyStatus.NEEDS_HUMAN:
            stats.escalated += 1
        else:
            stats.drafted += 1

    session.flush()
    return stats
