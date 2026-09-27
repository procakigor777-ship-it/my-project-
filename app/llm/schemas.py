"""Схемы структурированного вывода LLM.

Темы — закрытый enum, а не свободные строки: иначе агрегаты
рассыпаются на синонимы («краши», «вылеты», «crash») и дашборд
перестаёт что-либо значить.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class Topic(str, enum.Enum):
    CRASH = "crash"
    PERFORMANCE = "performance"
    BILLING = "billing"
    SUBSCRIPTION_CANCEL = "subscription_cancel"
    ONBOARDING = "onboarding"
    UI = "ui"
    MISSING_FEATURE = "missing_feature"
    SUPPORT = "support"
    ADS = "ads"
    PRIVACY = "privacy"
    ACCOUNT_LOGIN = "account_login"
    SYNC = "sync"
    PRAISE = "praise"
    OTHER = "other"


class RiskFlag(str, enum.Enum):
    LEGAL_THREAT = "legal_threat"
    SECURITY_REPORT = "security_report"
    APP_REVIEW_VIOLATION_CLAIM = "app_review_violation_claim"
    MEDIA_ATTENTION = "media_attention"
    SELF_HARM = "self_harm"


class SentimentValue(str, enum.Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class ReviewAnalysisResult(BaseModel):
    """То, что модель обязана вернуть по каждому отзыву."""

    sentiment: SentimentValue
    severity: int = Field(
        ge=1,
        le=5,
        description=(
            "1 — придирка или вкусовщина; 2 — мелкое неудобство; "
            "3 — сломан сценарий; 4 — потеря денег или данных; "
            "5 — публичная угроза, юридический риск, массовый сбой"
        ),
    )
    topics: list[Topic] = Field(default_factory=list, max_length=4)
    language: str = Field(description="Язык текста отзыва, код ISO 639-1")
    summary: str = Field(description="Суть отзыва одной фразой, на русском")

    is_actionable_bug: bool = Field(description="Описан воспроизводимый дефект")
    repro_hint: str | None = Field(
        default=None, description="Что именно делал пользователь, если это видно из текста"
    )
    affected_version: str | None = None
    requests_refund: bool = False
    mentions_competitor: bool = False
    contains_pii: bool = Field(
        description="В тексте есть телефон, email, адрес или иные персональные данные"
    )
    risk_flags: list[RiskFlag] = Field(default_factory=list)


class DraftResult(BaseModel):
    """Черновик ответа."""

    reply_text: str
    language: str
    escalate_to_human: bool = Field(
        default=False,
        description="Модель считает, что отвечать шаблонно нельзя и нужен человек",
    )
    escalation_reason: str | None = None


class DigestProblem(BaseModel):
    title: str
    description: str
    review_count: int
    affected_versions: list[str] = Field(default_factory=list)
    topics: list[Topic] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list, max_length=3)
    suspected_cause: str | None = None


class DigestResult(BaseModel):
    period: str
    headline: str
    problems: list[DigestProblem]
