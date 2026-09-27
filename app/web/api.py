"""HTTP-слой: очередь модерации и дашборд.

Аутентификации здесь нет намеренно: сервис рассчитан на закрытый
контур (VPN или reverse-proxy с авторизацией). Имя модератора
приходит заголовком X-Moderator и пишется в историю решений.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import analytics, moderation
from app.db import get_session_factory, init_db
from app.models import Reply, ReplyStatus

STATIC_DIR = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="Модерация ответов на отзывы", version="0.1.0", lifespan=lifespan)


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def moderator_name(x_moderator: str | None = Header(default=None)) -> str:
    return x_moderator or "unknown"


class ApproveRequest(BaseModel):
    text: str | None = Field(default=None, description="Правленый текст; None — публиковать черновик")


class RejectRequest(BaseModel):
    reason: str


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/queue")
def get_queue(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)) -> dict:
    items = moderation.queue(db, limit=limit, offset=offset)
    return {
        "items": [
            {
                "reply_id": item.reply.id,
                "status": item.reply.status.value,
                "draft_text": item.reply.draft_text,
                "char_limit": item.reply.char_limit,
                "note": item.reply.last_error,
                "review": {
                    "id": item.review.id,
                    "store": item.review.store.value,
                    "rating": item.review.rating,
                    "title": item.review.title,
                    "body": item.review.body,
                    "author": item.review.author,
                    "locale": item.review.locale,
                    "app_version": item.review.app_version,
                    "device": item.review.device,
                    "created_at": item.review.created_at.isoformat()
                    if item.review.created_at
                    else None,
                },
                "analysis": None
                if item.analysis is None
                else {
                    "sentiment": item.analysis.sentiment.value,
                    "severity": item.analysis.severity,
                    "summary": item.analysis.summary,
                    "language": item.analysis.language,
                    "topics": [t.topic for t in item.review.topics],
                    "is_actionable_bug": item.analysis.is_actionable_bug,
                    "repro_hint": item.analysis.repro_hint,
                    "requests_refund": item.analysis.requests_refund,
                    "contains_pii": item.analysis.contains_pii,
                    "risk_flags": item.analysis.risk_flags or [],
                },
            }
            for item in items
        ]
    }


@app.post("/api/replies/{reply_id}/approve")
def approve(
    reply_id: int,
    payload: ApproveRequest,
    db: Session = Depends(get_db),
    moderator: str = Depends(moderator_name),
) -> dict:
    try:
        reply = moderation.approve(db, reply_id, moderator, payload.text)
    except moderation.ModerationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"reply_id": reply.id, "status": reply.status.value, "edited": reply.was_edited}


@app.post("/api/replies/{reply_id}/reject")
def reject(
    reply_id: int,
    payload: RejectRequest,
    db: Session = Depends(get_db),
    moderator: str = Depends(moderator_name),
) -> dict:
    try:
        reply = moderation.reject(db, reply_id, moderator, payload.reason)
    except moderation.ModerationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"reply_id": reply.id, "status": reply.status.value}


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db)) -> dict:
    return analytics.dashboard(db)


@app.get("/api/alerts")
def alerts(db: Session = Depends(get_db)) -> dict:
    from app.alerts import run_all

    return {
        "alerts": [
            {"level": a.level, "title": a.title, "detail": a.detail} for a in run_all(db)
        ]
    }


@app.get("/api/reject-reasons")
def top_reject_reasons(days: int = 30, db: Session = Depends(get_db)) -> dict:
    return {"reasons": [{"reason": r, "count": c} for r, c in analytics.reject_reasons(db, days)]}


@app.get("/api/replies/{reply_id}")
def get_reply(reply_id: int, db: Session = Depends(get_db)) -> dict:
    reply = db.get(Reply, reply_id)
    if reply is None:
        raise HTTPException(status_code=404, detail="Ответ не найден")
    return {
        "reply_id": reply.id,
        "status": reply.status.value,
        "draft_text": reply.draft_text,
        "final_text": reply.final_text,
        "was_edited": reply.was_edited,
        "moderator": reply.moderator,
        "reject_reason": reply.reject_reason,
        "published_at": reply.published_at.isoformat() if reply.published_at else None,
        "store_response_id": reply.store_response_id,
        "attempts": reply.attempts,
        "last_error": reply.last_error,
    }


@app.get("/api/health")
def health(db: Session = Depends(get_db)) -> dict:
    queue_counts = analytics.queue_stats(db)
    return {
        "status": "ok",
        "pending": queue_counts.get(ReplyStatus.PENDING.value, 0),
        "needs_human": queue_counts.get(ReplyStatus.NEEDS_HUMAN.value, 0),
    }
