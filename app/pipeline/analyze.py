"""Шаг анализа: классификация отзывов.

Два режима:
  * sync  — по одному вызову на отзыв. Просто и сразу, для небольших объёмов.
  * batch — пачкой через Batch API: вдвое дешевле, но результат приходит
            асинхронно, поэтому submit и collect — разные вызовы.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.analysis import analyze_review, build_batch_items, review_id_from_custom_id
from app.llm.brand import BrandProfile
from app.llm.client import LLMClient, LLMError
from app.llm.prompts import ANALYSIS_PROMPT_VERSION, build_analysis_system
from app.llm.schemas import ReviewAnalysisResult
from app.models import (
    AnalysisBatch,
    Review,
    ReviewAnalysis,
    ReviewTopic,
    Sentiment,
    utcnow,
)

logger = logging.getLogger(__name__)


@dataclass
class AnalyzeStats:
    analyzed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def pending_reviews(session: Session, limit: int = 200) -> list[Review]:
    """Отзывы без анализа или с анализом по устаревшему тексту/промпту."""
    stmt = (
        select(Review)
        .outerjoin(ReviewAnalysis, ReviewAnalysis.review_id == Review.id)
        .where(
            (ReviewAnalysis.id.is_(None))
            | (ReviewAnalysis.source_body_hash != Review.body_hash)
            | (ReviewAnalysis.prompt_version != ANALYSIS_PROMPT_VERSION)
        )
        .order_by(Review.created_at.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().unique())


def analyze_sync(
    session: Session,
    llm: LLMClient,
    brand: BrandProfile,
    model: str,
    limit: int = 200,
) -> AnalyzeStats:
    stats = AnalyzeStats()
    for review in pending_reviews(session, limit):
        if not review.body.strip() and not (review.title or "").strip():
            stats.skipped += 1
            continue
        try:
            result = analyze_review(llm, brand, model, review)
        except LLMError as exc:
            logger.error("analyze review=%s: %s", review.id, exc)
            stats.errors.append(f"review {review.id}: {exc}")
            continue
        store_analysis(session, review, result, model)
        stats.analyzed += 1
    session.flush()
    return stats


def store_analysis(
    session: Session, review: Review, result: ReviewAnalysisResult, model: str
) -> ReviewAnalysis:
    """Пишет разметку и синхронизирует нормализованные темы."""
    analysis = review.analysis or ReviewAnalysis(review_id=review.id)
    analysis.sentiment = Sentiment(result.sentiment.value)
    analysis.severity = result.severity
    analysis.summary = result.summary
    analysis.language = result.language
    analysis.is_actionable_bug = result.is_actionable_bug
    analysis.repro_hint = result.repro_hint
    analysis.affected_version = result.affected_version
    analysis.requests_refund = result.requests_refund
    analysis.mentions_competitor = result.mentions_competitor
    analysis.contains_pii = result.contains_pii
    analysis.risk_flags = [f.value for f in result.risk_flags]
    analysis.model = model
    analysis.prompt_version = ANALYSIS_PROMPT_VERSION
    analysis.source_body_hash = review.body_hash
    analysis.analyzed_at = utcnow()

    if analysis.review_id is None:
        analysis.review_id = review.id
    session.add(analysis)
    review.analysis = analysis

    wanted = {t.value for t in result.topics}
    for existing in list(review.topics):
        if existing.topic not in wanted:
            session.delete(existing)
        else:
            wanted.discard(existing.topic)
    for topic in wanted:
        session.add(ReviewTopic(review_id=review.id, topic=topic))
    return analysis


# --- пакетный режим ---------------------------------------------------


def submit_batch(
    session: Session,
    llm: LLMClient,
    brand: BrandProfile,
    model: str,
    limit: int = 1000,
) -> AnalysisBatch | None:
    """Отправляет неразмеченные отзывы одним батчем."""
    reviews = [r for r in pending_reviews(session, limit) if r.body.strip() or (r.title or "").strip()]
    if not reviews:
        return None

    items = build_batch_items(reviews)
    batch_id = llm.parse_batch(
        model=model,
        system=build_analysis_system(brand),
        items=items,
        output_format=ReviewAnalysisResult,
        max_tokens=2000,
    )
    record = AnalysisBatch(
        provider_batch_id=batch_id,
        status="in_progress",
        review_ids=[r.id for r in reviews],
        model=model,
        prompt_version=ANALYSIS_PROMPT_VERSION,
    )
    session.add(record)
    session.flush()
    logger.info("batch %s отправлен, отзывов: %d", batch_id, len(reviews))
    return record


def collect_batches(session: Session, llm: LLMClient) -> AnalyzeStats:
    """Забирает результаты всех незакрытых батчей."""
    stats = AnalyzeStats()
    open_batches = list(
        session.execute(
            select(AnalysisBatch).where(AnalysisBatch.status == "in_progress")
        ).scalars()
    )
    for record in open_batches:
        status = getattr(llm, "batch_status", lambda _id: "ended")(record.provider_batch_id)
        if status != "ended":
            continue

        results = llm.collect_batch(record.provider_batch_id, ReviewAnalysisResult)
        for custom_id, outcome in results.items():
            review_id = review_id_from_custom_id(custom_id)
            if review_id is None:
                continue
            review = session.get(Review, review_id)
            if review is None:
                continue
            if isinstance(outcome, Exception):
                stats.errors.append(f"review {review_id}: {outcome}")
                continue
            store_analysis(session, review, outcome, record.model)
            stats.analyzed += 1

        record.status = "ended"
        record.completed_at = utcnow()
    session.flush()
    return stats
