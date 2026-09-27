"""Периодический запуск конвейера.

Вебхуков ни App Store, ни Google Play не дают — только опрос. Интервал
сбора для Google Play должен быть заметно меньше недели: всё, что не
успели забрать, API больше не отдаст.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from app.db import init_db, session_scope
from app.service import Services, build_services, run_cycle

logger = logging.getLogger(__name__)


def _cycle(services: Services, use_batch: bool) -> None:
    try:
        report = run_cycle(services, use_batch=use_batch)
        logger.info("цикл завершён: %s", report)
    except Exception:  # noqa: BLE001 — воркер не должен умирать от одного сбоя
        logger.exception("цикл упал")


def _alerts() -> None:
    from app.alerts import run_all

    try:
        with session_scope() as session:
            for alert in run_all(session):
                logger.warning("[%s] %s — %s", alert.level.upper(), alert.title, alert.detail)
    except Exception:  # noqa: BLE001
        logger.exception("проверка алертов упала")


def run(use_batch: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    services = build_services()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        _cycle,
        "interval",
        minutes=services.settings.pipeline_interval_minutes,
        args=[services, use_batch],
        next_run_time=None,
        id="pipeline",
    )
    scheduler.add_job(_alerts, "interval", minutes=60, id="alerts")

    logger.info(
        "воркер запущен: цикл каждые %s мин, публикация %s",
        services.settings.pipeline_interval_minutes,
        "включена" if services.settings.publish_enabled else "ВЫКЛЮЧЕНА",
    )
    _cycle(services, use_batch)
    scheduler.start()
