"""Недельная сводка по негативу.

Единственное место, где LLM используется для агрегации: нужна
группировка по смыслу, а не по строке. Раз в неделю, поэтому здесь
уместна самая сильная модель — стоимость незаметна, а качество
группировки определяет, попадёт ли проблема в бэклог.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.brand import BrandProfile
from app.llm.client import LLMClient
from app.llm.prompts import build_digest_system
from app.llm.schemas import DigestResult
from app.models import Review, ReviewAnalysis, Sentiment, utcnow

#: Ограничение на объём выборки: дальше растёт цена, а группировка
#: уже не улучшается. При больших объёмах отдавайте summary вместо тел.
MAX_REVIEWS = 400


def collect_negative(session: Session, days: int = 7) -> list[dict]:
    cutoff = utcnow() - timedelta(days=days)
    rows = list(
        session.execute(
            select(Review, ReviewAnalysis)
            .join(ReviewAnalysis, ReviewAnalysis.review_id == Review.id)
            .where(Review.created_at >= cutoff, ReviewAnalysis.sentiment == Sentiment.NEGATIVE)
            .order_by(ReviewAnalysis.severity.desc(), Review.created_at.desc())
            .limit(MAX_REVIEWS)
        ).all()
    )
    return [
        {
            "store": review.store.value,
            "rating": review.rating,
            "app_version": analysis.affected_version or review.app_version,
            "device": review.device,
            "severity": analysis.severity,
            "topics": [t.topic for t in review.topics],
            "summary": analysis.summary,
            "text": review.body[:600],
        }
        for review, analysis in rows
    ]


def build_digest(
    session: Session, llm: LLMClient, brand: BrandProfile, model: str, days: int = 7
) -> DigestResult | None:
    reviews = collect_negative(session, days)
    if not reviews:
        return None

    user = (
        f"Негативные отзывы за последние {days} дней ({len(reviews)} шт.). "
        "Сгруппируй их в отдельные проблемы.\n\n"
        + json.dumps(reviews, ensure_ascii=False, indent=2)
    )
    return llm.parse(
        model=model,
        system=build_digest_system(brand),
        user=user,
        output_format=DigestResult,
        max_tokens=8000,
    )


def render_markdown(digest: DigestResult) -> str:
    lines = [f"# Сводка по негативу: {digest.period}", "", digest.headline, ""]
    for i, problem in enumerate(digest.problems, start=1):
        lines.append(f"## {i}. {problem.title} ({problem.review_count} отзывов)")
        lines.append(problem.description)
        if problem.affected_versions:
            lines.append(f"**Версии:** {', '.join(problem.affected_versions)}")
        if problem.topics:
            lines.append(f"**Темы:** {', '.join(t.value for t in problem.topics)}")
        if problem.suspected_cause:
            lines.append(f"**Гипотеза о причине:** {problem.suspected_cause}")
        for quote in problem.quotes:
            lines.append(f"> {quote}")
        lines.append("")
    return "\n".join(lines)
