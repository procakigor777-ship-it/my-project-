"""Сквозные тесты конвейера на мок-магазинах и подменённой модели."""

from __future__ import annotations

import json

import pytest

from app import moderation
from app.models import ReplyStatus, Review, Store
from app.pipeline import analyze, draft, ingest, publish
from app.stores.base import ReviewGone


def run_to_drafts(session, clients, llm, brand, ceiling=4):
    ingest.ingest(session, clients)
    analyze.analyze_sync(session, llm, brand, "fake-model")
    return draft.generate_drafts(
        session, llm, brand, "fake-model", clients, severity_ceiling=ceiling
    )


def test_ingest_is_idempotent(session, clients):
    first = ingest.ingest(session, clients)
    assert first.created == 4

    second = ingest.ingest(session, clients)
    assert second.created == 0
    assert session.query(Review).count() == 4


def test_edited_review_resets_analysis_and_draft(session, clients, llm, brand, fixtures_dir):
    run_to_drafts(session, clients, llm, brand)
    review = session.query(Review).filter_by(store_review_id="gp-1").one()
    assert review.analysis is not None
    assert review.active_reply is not None

    # пользователь переписал отзыв
    path = fixtures_dir / "google_play.json"
    items = json.loads(path.read_text(encoding="utf-8"))
    items[0]["body"] = "Теперь всё работает, спасибо за фикс."
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")

    stats = ingest.ingest(session, clients)
    assert stats.updated == 1

    session.expire_all()
    review = session.query(Review).filter_by(store_review_id="gp-1").one()
    assert review.analysis is None, "анализ старого текста должен быть сброшен"
    assert all(r.status is ReplyStatus.OBSOLETE for r in review.replies)


def test_risky_review_never_gets_autodraft(session, clients, llm, brand):
    run_to_drafts(session, clients, llm, brand)

    risky = session.query(Review).filter_by(store_review_id="as-3").one()
    reply = risky.active_reply
    assert reply.status is ReplyStatus.NEEDS_HUMAN
    assert reply.draft_text is None, "на юридическую угрозу черновик не генерируется"
    assert "legal_threat" in reply.last_error


def test_draft_created_for_ordinary_review(session, clients, llm, brand):
    stats = run_to_drafts(session, clients, llm, brand)
    assert stats.drafted >= 1

    ordinary = session.query(Review).filter_by(store_review_id="gp-1").one()
    assert ordinary.active_reply.status is ReplyStatus.PENDING
    assert ordinary.active_reply.draft_text


def test_play_char_limit_enforced_in_two_steps(session, clients, llm, brand):
    """Слишком длинный текст должен упереться в лимит, а не уехать в магазин."""
    llm.forced_draft = "а" * 900  # лимит Google Play — 350
    run_to_drafts(session, clients, llm, brand)

    review = session.query(Review).filter_by(store_review_id="gp-1").one()
    reply = review.active_reply
    assert reply.char_limit == 350
    assert reply.status is ReplyStatus.NEEDS_HUMAN
    assert len(reply.draft_text) <= 350


def test_moderation_rejects_text_over_limit(session, clients, llm, brand):
    run_to_drafts(session, clients, llm, brand)
    review = session.query(Review).filter_by(store_review_id="gp-1").one()

    with pytest.raises(moderation.ModerationError, match="лимита"):
        moderation.approve(session, review.active_reply.id, "tester", "я" * 400)


def test_reject_requires_reason(session, clients, llm, brand):
    run_to_drafts(session, clients, llm, brand)
    review = session.query(Review).filter_by(store_review_id="gp-1").one()

    with pytest.raises(moderation.ModerationError):
        moderation.reject(session, review.active_reply.id, "tester", "   ")


def test_publish_requires_kill_switch_on(session, clients, llm, brand):
    run_to_drafts(session, clients, llm, brand)
    review = session.query(Review).filter_by(store_review_id="gp-1").one()
    moderation.approve(session, review.active_reply.id, "tester")

    off = publish.publish(session, clients, enabled=False)
    assert off.published == 0

    on = publish.publish(session, clients, enabled=True)
    assert on.published == 1

    session.expire_all()
    review = session.query(Review).filter_by(store_review_id="gp-1").one()
    assert review.active_reply.status is ReplyStatus.PUBLISHED
    assert review.active_reply.store_response_id


def test_publish_is_idempotent(session, clients, llm, brand):
    run_to_drafts(session, clients, llm, brand)
    review = session.query(Review).filter_by(store_review_id="gp-1").one()
    moderation.approve(session, review.active_reply.id, "tester")

    assert publish.publish(session, clients, enabled=True).published == 1
    assert publish.publish(session, clients, enabled=True).published == 0


def test_edit_is_tracked_separately_from_draft(session, clients, llm, brand):
    run_to_drafts(session, clients, llm, brand)
    review = session.query(Review).filter_by(store_review_id="gp-1").one()
    reply = moderation.approve(session, review.active_reply.id, "tester", "Свой текст ответа.")

    assert reply.draft_text != reply.final_text
    assert reply.was_edited is True


def test_deleted_review_is_not_a_failure(session, clients, llm, brand, monkeypatch):
    run_to_drafts(session, clients, llm, brand)
    review = session.query(Review).filter_by(store_review_id="gp-1").one()
    moderation.approve(session, review.active_reply.id, "tester")

    def gone(*_args, **_kwargs):
        raise ReviewGone()

    monkeypatch.setattr(clients[Store.GOOGLE_PLAY], "publish_reply", gone)
    stats = publish.publish(session, clients, enabled=True)

    assert stats.gone == 1 and stats.failed == 0
    session.expire_all()
    review = session.query(Review).filter_by(store_review_id="gp-1").one()
    assert all(r.status is ReplyStatus.OBSOLETE for r in review.replies)


def test_retryable_error_keeps_reply_in_queue(session, clients, llm, brand, monkeypatch):
    from app.stores.base import StoreError

    run_to_drafts(session, clients, llm, brand)
    review = session.query(Review).filter_by(store_review_id="gp-1").one()
    moderation.approve(session, review.active_reply.id, "tester")

    def flaky(*_args, **_kwargs):
        raise StoreError("503", retryable=True, status=503)

    monkeypatch.setattr(clients[Store.GOOGLE_PLAY], "publish_reply", flaky)
    stats = publish.publish(session, clients, enabled=True)

    assert stats.retry_later == 1 and stats.failed == 0
    session.expire_all()
    review = session.query(Review).filter_by(store_review_id="gp-1").one()
    assert review.active_reply.status is ReplyStatus.APPROVED


def test_batch_analysis_matches_by_custom_id(session, clients, llm, brand):
    ingest.ingest(session, clients)
    record = analyze.submit_batch(session, llm, brand, "fake-model")
    assert record is not None

    stats = analyze.collect_batches(session, llm)
    assert stats.analyzed == 4
    assert all(r.analysis is not None for r in session.query(Review).all())
