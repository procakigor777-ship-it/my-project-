"""Модель данных.

Четыре сущности: сырые отзывы, их анализ, черновики ответов и батчи анализа.
Плюс нормализованные темы — по ним строятся агрегаты, JSON-поле для этого
неудобно (особенно на SQLite).
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite не хранит смещение — всё, что пришло из БД, трактуем как UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class Store(str, enum.Enum):
    APP_STORE = "app_store"
    GOOGLE_PLAY = "google_play"


class Sentiment(str, enum.Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class ReplyStatus(str, enum.Enum):
    PENDING = "pending"          # черновик готов, ждёт модератора
    NEEDS_HUMAN = "needs_human"  # severity/risk — черновик намеренно не генерировался
    APPROVED = "approved"        # модератор подтвердил, ждёт публикатора
    REJECTED = "rejected"        # решено не отвечать
    PUBLISHED = "published"
    FAILED = "failed"            # магазин отклонил, вернулось модератору
    OBSOLETE = "obsolete"        # отзыв отредактирован или удалён — черновик неактуален


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("store", "store_review_id", name="uq_review_store_id"),
        Index("ix_reviews_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store: Mapped[Store] = mapped_column(Enum(Store), nullable=False)
    store_review_id: Mapped[str] = mapped_column(String(128), nullable=False)

    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Хеш текста: если пользователь отредактировал отзыв, анализ и черновик
    # сбрасываются и считаются заново.
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    author: Mapped[str | None] = mapped_column(String(256))

    locale: Mapped[str | None] = mapped_column(String(16))
    territory: Mapped[str | None] = mapped_column(String(8))
    app_version: Mapped[str | None] = mapped_column(String(64))
    device: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    raw: Mapped[dict | None] = mapped_column(JSON)

    analysis: Mapped["ReviewAnalysis | None"] = relationship(
        back_populates="review", uselist=False, cascade="all, delete-orphan"
    )
    replies: Mapped[list["Reply"]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )
    topics: Mapped[list["ReviewTopic"]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )

    @property
    def active_reply(self) -> "Reply | None":
        live = [r for r in self.replies if r.status != ReplyStatus.OBSOLETE]
        return max(live, key=lambda r: r.id) if live else None


class ReviewAnalysis(Base):
    __tablename__ = "review_analysis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[int] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    sentiment: Mapped[Sentiment] = mapped_column(Enum(Sentiment), nullable=False)
    severity: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str | None] = mapped_column(String(16))

    is_actionable_bug: Mapped[bool] = mapped_column(Boolean, default=False)
    repro_hint: Mapped[str | None] = mapped_column(Text)
    affected_version: Mapped[str | None] = mapped_column(String(64))
    requests_refund: Mapped[bool] = mapped_column(Boolean, default=False)
    mentions_competitor: Mapped[bool] = mapped_column(Boolean, default=False)
    contains_pii: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_flags: Mapped[list | None] = mapped_column(JSON, default=list)

    # Версии нужны, чтобы понимать, что считалось по старому промпту,
    # и уметь пересчитать выборочно.
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    source_body_hash: Mapped[str] = mapped_column(String(64), default="")
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    review: Mapped[Review] = relationship(back_populates="analysis")


class ReviewTopic(Base):
    __tablename__ = "review_topics"
    __table_args__ = (
        UniqueConstraint("review_id", "topic", name="uq_review_topic"),
        Index("ix_review_topics_topic", "topic"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[int] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(64), nullable=False)

    review: Mapped[Review] = relationship(back_populates="topics")


class Reply(Base):
    __tablename__ = "replies"
    __table_args__ = (Index("ix_replies_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[int] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False
    )

    draft_text: Mapped[str | None] = mapped_column(Text)
    final_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ReplyStatus] = mapped_column(
        Enum(ReplyStatus), default=ReplyStatus.PENDING, nullable=False
    )

    moderator: Mapped[str | None] = mapped_column(String(128))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    moderated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    char_limit: Mapped[int | None] = mapped_column(Integer)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    store_response_id: Mapped[str | None] = mapped_column(String(128))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    review: Mapped[Review] = relationship(back_populates="replies")

    @property
    def text_to_publish(self) -> str | None:
        return self.final_text or self.draft_text

    @property
    def was_edited(self) -> bool:
        """Правил ли модератор черновик — главный сигнал качества генерации."""
        return bool(self.final_text and self.draft_text and self.final_text != self.draft_text)


class AnalysisBatch(Base):
    """Отслеживание батча анализа в Batch API (обработка идёт асинхронно)."""

    __tablename__ = "analysis_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_batch_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="in_progress")
    review_ids: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
