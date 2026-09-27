"""Адаптеры магазинов приложений."""

from __future__ import annotations

from app.config import Settings, get_settings
from app.models import Store
from app.stores.base import StoreClient, StoreError


def build_clients(settings: Settings | None = None) -> dict[Store, StoreClient]:
    """Собирает набор активных адаптеров по конфигу."""
    settings = settings or get_settings()

    if settings.store_mode == "mock":
        from app.stores.mock import MockStoreClient

        return {
            Store.APP_STORE: MockStoreClient(Store.APP_STORE),
            Store.GOOGLE_PLAY: MockStoreClient(Store.GOOGLE_PLAY),
        }

    clients: dict[Store, StoreClient] = {}
    if settings.appstore_enabled:
        from app.stores.appstore import AppStoreClient

        clients[Store.APP_STORE] = AppStoreClient(settings)
    if settings.googleplay_enabled:
        from app.stores.googleplay import GooglePlayClient

        clients[Store.GOOGLE_PLAY] = GooglePlayClient(settings)
    return clients


__all__ = ["StoreClient", "StoreError", "build_clients"]
