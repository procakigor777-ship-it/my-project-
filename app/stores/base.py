"""Общий контракт адаптера магазина."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from app.models import Store


class StoreError(Exception):
    """Ошибка похода в магазин.

    `retryable=True` — сетевой сбой или 5xx, публикатор попробует снова.
    `retryable=False` — 4xx: текст не принят, отзыв удалён и т.п.
    Такое возвращается модератору, а не ретраится.
    """

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ReviewGone(StoreError):
    """Отзыв удалён между генерацией черновика и публикацией. Это норма."""

    def __init__(self, message: str = "review no longer exists"):
        super().__init__(message, retryable=False, status=404)


@dataclass(slots=True)
class FetchedReview:
    """Отзыв в форме, не зависящей от магазина."""

    store: Store
    store_review_id: str
    rating: int
    body: str
    created_at: datetime
    title: str | None = None
    author: str | None = None
    locale: str | None = None
    territory: str | None = None
    app_version: str | None = None
    device: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def body_hash(self) -> str:
        payload = f"{self.title or ''}\n{self.body}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class StoreClient(ABC):
    """Адаптер одного магазина."""

    store: Store

    #: Жёсткий лимит длины ответа на стороне магазина.
    reply_char_limit: int = 1000

    @abstractmethod
    def fetch_reviews(self, since: datetime | None = None) -> list[FetchedReview]:
        """Возвращает отзывы, появившиеся или изменившиеся после `since`."""

    @abstractmethod
    def publish_reply(self, store_review_id: str, text: str) -> str:
        """Публикует ответ и возвращает идентификатор ответа в магазине."""
