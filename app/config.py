"""Конфигурация сервиса. Читается из окружения и/или .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    anthropic_api_key: str | None = None
    model_analysis: str = "claude-haiku-4-5"
    model_draft: str = "claude-sonnet-5"
    model_digest: str = "claude-opus-5"

    # Хранилище
    database_url: str = "sqlite:///./reviews.db"

    # Рубильник: пока False, публикатор не отправляет ничего в магазины.
    publish_enabled: bool = False

    # mock — работа на фикстурах без единого реального ключа; real — боевые API.
    store_mode: str = "mock"

    # App Store Connect
    appstore_enabled: bool = False
    appstore_issuer_id: str | None = None
    appstore_key_id: str | None = None
    appstore_private_key_path: Path | None = None
    appstore_app_id: str | None = None
    # Пустая строка = опрашивать все территории.
    appstore_territories: str = ""

    # Google Play
    googleplay_enabled: bool = False
    googleplay_service_account_path: Path | None = None
    googleplay_package_name: str | None = None

    # Бренд
    brand_profile_path: Path = Path("./brand.json")

    # Расписание
    ingest_interval_minutes: int = 30
    pipeline_interval_minutes: int = 10

    # Отзывы с такой серьёзностью и выше не получают авточерновик —
    # только карточку для человека.
    draft_severity_ceiling: int = 4

    @property
    def appstore_territory_list(self) -> list[str]:
        return [t.strip().upper() for t in self.appstore_territories.split(",") if t.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
