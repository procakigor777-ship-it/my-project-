"""Сборка зависимостей и полный цикл конвейера."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import Settings, get_settings
from app.db import session_scope
from app.llm.brand import BrandProfile
from app.llm.client import LLMClient, build_llm
from app.models import Store
from app.pipeline import analyze, draft, ingest, publish
from app.stores import build_clients
from app.stores.base import StoreClient

logger = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    brand: BrandProfile
    clients: dict[Store, StoreClient]
    _llm: LLMClient | None = field(default=None, repr=False)

    @property
    def llm(self) -> LLMClient:
        """LLM создаётся лениво: сбор отзывов должен работать и без ключа."""
        if self._llm is None:
            self._llm = build_llm(self.settings.anthropic_api_key)
        return self._llm


def build_services(
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    clients: dict[Store, StoreClient] | None = None,
) -> Services:
    settings = settings or get_settings()
    return Services(
        settings=settings,
        brand=BrandProfile.load(settings.brand_profile_path),
        clients=clients if clients is not None else build_clients(settings),
        _llm=llm,
    )


def run_cycle(services: Services, use_batch: bool = False) -> dict:
    """Один полный проход: сбор → анализ → черновики → публикация.

    Шаги разделены состоянием в БД, поэтому сбой любого из них не
    приводит к публикации мусора и не требует перезапуска предыдущих.
    """
    report: dict = {}

    with session_scope() as session:
        stats = ingest.ingest(session, services.clients)
        report["ingest"] = {
            "created": stats.created,
            "updated": stats.updated,
            "unchanged": stats.unchanged,
            "errors": stats.errors,
        }

    with session_scope() as session:
        if use_batch:
            collected = analyze.collect_batches(session, services.llm)
            submitted = analyze.submit_batch(
                session, services.llm, services.brand, services.settings.model_analysis
            )
            report["analyze"] = {
                "collected": collected.analyzed,
                "submitted_batch": submitted.provider_batch_id if submitted else None,
                "errors": collected.errors,
            }
        else:
            stats = analyze.analyze_sync(
                session, services.llm, services.brand, services.settings.model_analysis
            )
            report["analyze"] = {
                "analyzed": stats.analyzed,
                "skipped": stats.skipped,
                "errors": stats.errors,
            }

    with session_scope() as session:
        stats = draft.generate_drafts(
            session,
            services.llm,
            services.brand,
            services.settings.model_draft,
            services.clients,
            severity_ceiling=services.settings.draft_severity_ceiling,
        )
        report["draft"] = {
            "drafted": stats.drafted,
            "escalated": stats.escalated,
            "skipped": stats.skipped,
            "errors": stats.errors,
        }

    with session_scope() as session:
        stats = publish.publish(
            session, services.clients, enabled=services.settings.publish_enabled
        )
        report["publish"] = {
            "published": stats.published,
            "failed": stats.failed,
            "retry_later": stats.retry_later,
            "gone": stats.gone,
            "errors": stats.errors,
        }

    return report
