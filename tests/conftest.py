from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Тесты не должны создавать файл БД в рабочей директории.
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("STORE_MODE", "mock")

from app.llm.brand import BrandProfile
from app.llm.schemas import (
    DraftResult,
    ReviewAnalysisResult,
    RiskFlag,
    SentimentValue,
    Topic,
)
from app.models import Base, Store
from app.stores.mock import MockStoreClient


@pytest.fixture
def session():
    # StaticPool + check_same_thread: TestClient выполняет запросы в
    # отдельном потоке, а база живёт в памяти одного соединения.
    engine = create_engine(
        "sqlite://",
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as s:
        yield s


@pytest.fixture
def brand() -> BrandProfile:
    return BrandProfile(product_name="Тест", tone_rules=["коротко"], signature="Команда")


class FakeLLM:
    """Детерминированная подмена модели.

    Правила намеренно примитивные: тесты проверяют конвейер, а не
    качество классификации.
    """

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.forced_draft: str | None = None
        self.batches: dict[str, list[tuple[str, str]]] = {}

    def parse(self, *, model, system, user, output_format, max_tokens=4000):
        self.calls.append((model, output_format.__name__))
        if output_format is ReviewAnalysisResult:
            return self._analysis(user)
        if output_format is DraftResult:
            text = self.forced_draft if self.forced_draft is not None else "Спасибо за отзыв."
            return DraftResult(reply_text=text, language="ru")
        raise AssertionError(f"Неожиданная схема: {output_format}")

    @staticmethod
    def _analysis(user: str) -> ReviewAnalysisResult:
        payload = json.loads(user[user.index("{") :])
        rating = payload.get("rating", 3)
        body = (payload.get("body") or "") + (payload.get("title") or "")
        risk = [RiskFlag.LEGAL_THREAT] if "суд" in body.lower() else []
        return ReviewAnalysisResult(
            sentiment=SentimentValue.NEGATIVE if rating <= 2 else SentimentValue.POSITIVE,
            severity=5 if risk else (4 if rating == 1 else 2),
            topics=[Topic.CRASH] if "вылет" in body.lower() or "краш" in body.lower() else [Topic.OTHER],
            language="ru",
            summary="сводка",
            is_actionable_bug=rating <= 2,
            repro_hint=None,
            affected_version=payload.get("app_version_reported_by_store"),
            requests_refund=False,
            mentions_competitor=False,
            contains_pii="@" in body,
            risk_flags=risk,
        )

    # --- батчи -------------------------------------------------------

    def parse_batch(self, *, model, system, items, output_format, max_tokens=4000):
        batch_id = f"batch-{len(self.batches) + 1}"
        self.batches[batch_id] = list(items)
        return batch_id

    def batch_status(self, batch_id: str) -> str:
        return "ended"

    def collect_batch(self, batch_id: str, output_format):
        return {custom_id: self._analysis(user) for custom_id, user in self.batches[batch_id]}


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def fixtures_dir(tmp_path: Path) -> Path:
    apple = [
        {
            "id": "as-1",
            "rating": 1,
            "title": "Вылетает",
            "body": "После обновления приложение вылетает при открытии ленты.",
            "author": "Дмитрий",
            "territory": "RUS",
            "locale": "ru",
            "app_version": "4.2.0",
            "created_at": "2026-09-25T08:45:00+00:00",
        },
        {
            "id": "as-2",
            "rating": 5,
            "title": "Супер",
            "body": "Всё работает отлично.",
            "author": "Ирина",
            "territory": "RUS",
            "locale": "ru",
            "app_version": "4.2.0",
            "created_at": "2026-09-24T10:00:00+00:00",
        },
        {
            "id": "as-3",
            "rating": 1,
            "title": "Подам в суд",
            "body": "Вы сливаете мои данные, обращусь в суд.",
            "author": "Аноним",
            "territory": "RUS",
            "locale": "ru",
            "app_version": "4.2.0",
            "created_at": "2026-09-26T12:00:00+00:00",
        },
    ]
    play = [
        {
            "id": "gp-1",
            "rating": 2,
            "body": "Интерфейс стал хуже, кнопку поиска не найти.",
            "author": "Олег",
            "locale": "ru",
            "app_version": "4.2.0",
            "device": "Redmi",
            "created_at": "2026-09-26T06:05:00+00:00",
        }
    ]
    (tmp_path / "app_store.json").write_text(json.dumps(apple, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "google_play.json").write_text(json.dumps(play, ensure_ascii=False), encoding="utf-8")
    return tmp_path


@pytest.fixture
def clients(fixtures_dir: Path, tmp_path: Path):
    runtime = tmp_path / "runtime"
    return {
        Store.APP_STORE: MockStoreClient(Store.APP_STORE, fixtures_dir, runtime),
        Store.GOOGLE_PLAY: MockStoreClient(Store.GOOGLE_PLAY, fixtures_dir, runtime),
    }
