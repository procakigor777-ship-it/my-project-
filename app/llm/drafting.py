"""Генерация черновика ответа."""

from __future__ import annotations

import json

from app.llm.brand import BrandProfile
from app.llm.client import LLMClient
from app.llm.prompts import build_draft_system
from app.llm.schemas import DraftResult
from app.models import Review, ReviewAnalysis, Store

STORE_NAMES = {Store.APP_STORE: "App Store", Store.GOOGLE_PLAY: "Google Play"}

#: Просим модель уложиться с запасом: лимит магазина жёсткий, а
#: «почти уложился» означает отклонённую публикацию.
LIMIT_SAFETY_MARGIN = 0.85


def render_draft_input(review: Review, analysis: ReviewAnalysis) -> str:
    payload = {
        "review": {
            "rating": review.rating,
            "title": review.title,
            "body": review.body,
            "language": analysis.language or review.locale,
            "app_version": analysis.affected_version or review.app_version,
            "device": review.device,
        },
        "analysis": {
            "sentiment": analysis.sentiment.value,
            "severity": analysis.severity,
            "topics": [t.topic for t in review.topics],
            "summary": analysis.summary,
            "is_actionable_bug": analysis.is_actionable_bug,
            "repro_hint": analysis.repro_hint,
            "requests_refund": analysis.requests_refund,
            "contains_pii": analysis.contains_pii,
            "risk_flags": analysis.risk_flags or [],
        },
    }
    return (
        "Напиши публичный ответ на этот отзыв. Учитывай разметку — она "
        "сделана заранее и ей можно доверять.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def draft_reply(
    llm: LLMClient,
    brand: BrandProfile,
    model: str,
    review: Review,
    analysis: ReviewAnalysis,
    char_limit: int,
) -> DraftResult:
    """Генерирует черновик и следит за лимитом длины.

    Лимит проверяется кодом, а не доверием к модели: при переборе даём
    ровно одну попытку переписать короче, дальше отдаём человеку.
    """
    target = int(char_limit * LIMIT_SAFETY_MARGIN)
    system = build_draft_system(brand, STORE_NAMES[review.store], target)
    user = render_draft_input(review, analysis)

    result = llm.parse(
        model=model, system=system, user=user, output_format=DraftResult, max_tokens=2000
    )
    if len(result.reply_text) <= char_limit:
        return result

    retry_user = (
        f"{user}\n\nПредыдущая версия ответа не прошла: {len(result.reply_text)} символов "
        f"при лимите {char_limit}. Перепиши короче, уложись в {target} символов, "
        "сохранив главное — признание проблемы и следующий шаг."
    )
    result = llm.parse(
        model=model, system=system, user=retry_user, output_format=DraftResult, max_tokens=2000
    )
    if len(result.reply_text) > char_limit:
        return DraftResult(
            reply_text=result.reply_text[:char_limit],
            language=result.language,
            escalate_to_human=True,
            escalation_reason=(
                f"Модель дважды не уложилась в лимит {char_limit} символов — "
                "текст обрезан, нужна ручная правка"
            ),
        )
    return result
