"""Анализ отзыва: классификация со структурированным выводом."""

from __future__ import annotations

import json
from typing import Iterable, Sequence

from app.llm.brand import BrandProfile
from app.llm.client import LLMClient
from app.llm.prompts import build_analysis_system
from app.llm.schemas import ReviewAnalysisResult
from app.models import Review

CUSTOM_ID_PREFIX = "review-"


def render_review(review: Review) -> str:
    """Переменная часть промпта — только данные отзыва.

    Идёт в user-сообщение, то есть после точки кэширования системного
    промпта.
    """
    payload = {
        "store": review.store.value,
        "rating": review.rating,
        "title": review.title,
        "body": review.body,
        "store_locale": review.locale,
        "territory": review.territory,
        "app_version_reported_by_store": review.app_version,
        "device": review.device,
        "created_at": review.created_at.isoformat() if review.created_at else None,
    }
    return "Размести этот отзыв по схеме:\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def analyze_review(
    llm: LLMClient, brand: BrandProfile, model: str, review: Review
) -> ReviewAnalysisResult:
    return llm.parse(
        model=model,
        system=build_analysis_system(brand),
        user=render_review(review),
        output_format=ReviewAnalysisResult,
        max_tokens=2000,
    )


def custom_id_for(review_id: int) -> str:
    return f"{CUSTOM_ID_PREFIX}{review_id}"


def review_id_from_custom_id(custom_id: str) -> int | None:
    if not custom_id.startswith(CUSTOM_ID_PREFIX):
        return None
    try:
        return int(custom_id[len(CUSTOM_ID_PREFIX) :])
    except ValueError:
        return None


def build_batch_items(reviews: Iterable[Review]) -> Sequence[tuple[str, str]]:
    return [(custom_id_for(r.id), render_review(r)) for r in reviews]
