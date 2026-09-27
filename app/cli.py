"""Командная строка: каждый шаг конвейера можно запустить отдельно."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app.config import get_settings
from app.db import init_db, session_scope
from app.service import build_services, run_cycle


def _print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def cmd_init_db(_args) -> int:
    init_db()
    print("Схема создана")
    return 0


def cmd_ingest(_args) -> int:
    from app.pipeline import ingest

    services = build_services()
    with session_scope() as session:
        stats = ingest.ingest(session, services.clients)
    _print(stats.__dict__)
    return 1 if stats.errors else 0


def cmd_analyze(args) -> int:
    from app.pipeline import analyze

    services = build_services()
    with session_scope() as session:
        if args.batch_submit:
            record = analyze.submit_batch(
                session, services.llm, services.brand, services.settings.model_analysis
            )
            _print({"batch": record.provider_batch_id if record else None})
        elif args.batch_collect:
            stats = analyze.collect_batches(session, services.llm)
            _print(stats.__dict__)
        else:
            stats = analyze.analyze_sync(
                session, services.llm, services.brand, services.settings.model_analysis
            )
            _print(stats.__dict__)
    return 0


def cmd_draft(_args) -> int:
    from app.pipeline import draft

    services = build_services()
    with session_scope() as session:
        stats = draft.generate_drafts(
            session,
            services.llm,
            services.brand,
            services.settings.model_draft,
            services.clients,
            severity_ceiling=services.settings.draft_severity_ceiling,
        )
    _print(stats.__dict__)
    return 0


def cmd_publish(_args) -> int:
    from app.pipeline import publish

    services = build_services()
    if not services.settings.publish_enabled:
        print("PUBLISH_ENABLED=false — публикация выключена, ничего не отправлено", file=sys.stderr)
    with session_scope() as session:
        stats = publish.publish(
            session, services.clients, enabled=services.settings.publish_enabled
        )
    _print(stats.__dict__)
    return 0


def cmd_cycle(args) -> int:
    init_db()
    services = build_services()
    _print(run_cycle(services, use_batch=args.batch))
    return 0


def cmd_queue(_args) -> int:
    from app import moderation

    with session_scope() as session:
        for item in moderation.queue(session, limit=20):
            severity = item.analysis.severity if item.analysis else "—"
            print(
                f"#{item.reply.id} [{item.reply.status.value}] "
                f"{item.review.store.value} {item.review.rating}★ severity={severity}"
            )
            print(f"   отзыв: {(item.review.body or '')[:120]}")
            print(f"   черновик: {(item.reply.draft_text or '— нет —')[:160]}\n")
    return 0


def cmd_approve(args) -> int:
    from app import moderation

    with session_scope() as session:
        reply = moderation.approve(session, args.reply_id, args.moderator, args.text)
    print(f"#{reply.id} → {reply.status.value}")
    return 0


def cmd_reject(args) -> int:
    from app import moderation

    with session_scope() as session:
        reply = moderation.reject(session, args.reply_id, args.moderator, args.reason)
    print(f"#{reply.id} → {reply.status.value}")
    return 0


def cmd_stats(_args) -> int:
    from app import analytics

    with session_scope() as session:
        _print(analytics.dashboard(session))
    return 0


def cmd_alerts(_args) -> int:
    from app.alerts import run_all

    with session_scope() as session:
        alerts = run_all(session)
    for alert in alerts:
        print(f"[{alert.level.upper()}] {alert.title} — {alert.detail}")
    if not alerts:
        print("Активных алертов нет")
    return 0


def cmd_digest(args) -> int:
    from app.pipeline import digest

    services = build_services()
    with session_scope() as session:
        result = digest.build_digest(
            session, services.llm, services.brand, services.settings.model_digest, days=args.days
        )
    if result is None:
        print("Негативных отзывов за период нет")
        return 0
    print(digest.render_markdown(result))
    return 0


def cmd_eval(args) -> int:
    """Прогон по размеченному набору: показывает, стало лучше или хуже."""
    from pathlib import Path

    from app import evaluation

    services = build_services()
    with session_scope() as session:
        report = evaluation.run_eval(
            session,
            services.llm,
            services.brand,
            args.model or services.settings.model_analysis,
            path=Path(args.path),
            limit=args.limit,
        )
    if args.json:
        _print(report.as_dict())
    else:
        print(evaluation.render(report))
    if args.min_accuracy and report.overall < args.min_accuracy:
        print(
            f"\nТочность {report.overall:.1%} ниже порога {args.min_accuracy:.1%}",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_examples(_args) -> int:
    """Выгружает опубликованные ответы — материал для few-shot примеров."""
    from app import moderation

    with session_scope() as session:
        _print(moderation.good_examples(session))
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    init_db()
    uvicorn.run("app.web.api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_worker(args) -> int:
    from app import scheduler

    scheduler.run(use_batch=args.batch)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reviews", description="Ответы на отзывы с модерацией")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="создать схему БД").set_defaults(func=cmd_init_db)
    sub.add_parser("ingest", help="забрать новые отзывы").set_defaults(func=cmd_ingest)

    p_analyze = sub.add_parser("analyze", help="разметить отзывы")
    group = p_analyze.add_mutually_exclusive_group()
    group.add_argument("--batch-submit", action="store_true", help="отправить пачку в Batch API")
    group.add_argument("--batch-collect", action="store_true", help="забрать результаты батчей")
    p_analyze.set_defaults(func=cmd_analyze)

    sub.add_parser("draft", help="сгенерировать черновики").set_defaults(func=cmd_draft)
    sub.add_parser("publish", help="опубликовать подтверждённые ответы").set_defaults(func=cmd_publish)

    p_cycle = sub.add_parser("cycle", help="полный проход конвейера")
    p_cycle.add_argument("--batch", action="store_true", help="анализ через Batch API")
    p_cycle.set_defaults(func=cmd_cycle)

    sub.add_parser("queue", help="показать очередь модерации").set_defaults(func=cmd_queue)

    p_approve = sub.add_parser("approve", help="подтвердить ответ")
    p_approve.add_argument("reply_id", type=int)
    p_approve.add_argument("--text", default=None, help="правленый текст")
    p_approve.add_argument("--moderator", default="cli")
    p_approve.set_defaults(func=cmd_approve)

    p_reject = sub.add_parser("reject", help="отклонить черновик")
    p_reject.add_argument("reply_id", type=int)
    p_reject.add_argument("reason")
    p_reject.add_argument("--moderator", default="cli")
    p_reject.set_defaults(func=cmd_reject)

    sub.add_parser("stats", help="агрегаты по отзывам").set_defaults(func=cmd_stats)
    sub.add_parser("alerts", help="проверить правила алертов").set_defaults(func=cmd_alerts)
    sub.add_parser("examples", help="выгрузить опубликованные ответы").set_defaults(func=cmd_examples)

    p_eval = sub.add_parser("eval", help="проверить качество классификации на разметке")
    p_eval.add_argument("--path", default="fixtures/eval_set.json")
    p_eval.add_argument("--model", default=None, help="переопределить модель анализа")
    p_eval.add_argument("--limit", type=int, default=None)
    p_eval.add_argument("--json", action="store_true")
    p_eval.add_argument(
        "--min-accuracy", type=float, default=None, help="код возврата 1, если точность ниже"
    )
    p_eval.set_defaults(func=cmd_eval)

    p_digest = sub.add_parser("digest", help="недельная сводка по негативу")
    p_digest.add_argument("--days", type=int, default=7)
    p_digest.set_defaults(func=cmd_digest)

    p_serve = sub.add_parser("serve", help="запустить админку модерации")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_worker = sub.add_parser("worker", help="фоновый воркер по расписанию")
    p_worker.add_argument("--batch", action="store_true")
    p_worker.set_defaults(func=cmd_worker)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    get_settings()  # ранняя валидация конфига
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
