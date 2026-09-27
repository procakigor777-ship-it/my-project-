from __future__ import annotations

from app import alerts, analytics, moderation
from app.models import Review, Store
from app.pipeline import analyze, draft, ingest


def _prepare(session, clients, llm, brand):
    ingest.ingest(session, clients)
    analyze.analyze_sync(session, llm, brand, "fake-model")
    draft.generate_drafts(session, llm, brand, "fake-model", clients)
    session.commit()


def test_dashboard_counts_negative(session, clients, llm, brand):
    _prepare(session, clients, llm, brand)
    data = analytics.dashboard(session)

    assert data["week"]["reviews"] == 4
    assert data["week"]["negative"] == 3  # три отзыва с оценкой <= 2
    assert data["versions"][0]["app_version"] == "4.2.0"


def test_topics_are_normalized_rows(session, clients, llm, brand):
    _prepare(session, clients, llm, brand)
    topics = dict(analytics.topics_breakdown(session, days=7))
    assert topics.get("crash", 0) >= 1


def test_risk_flag_raises_alert(session, clients, llm, brand):
    _prepare(session, clients, llm, brand)
    found = alerts.check_risk_flags(session)

    assert found and found[0].level == "critical"
    assert "legal_threat" in found[0].detail


def test_moderation_quality_tracks_edits(session, clients, llm, brand):
    _prepare(session, clients, llm, brand)
    review = session.query(Review).filter_by(store_review_id="gp-1").one()
    moderation.approve(session, review.active_reply.id, "tester", "Правленый текст.")
    session.commit()

    quality = analytics.moderation_quality(session)
    assert quality["moderated"] == 1
    assert quality["edited"] == 1
    assert quality["clean_rate"] == 0.0


def test_reject_reasons_are_collected(session, clients, llm, brand):
    _prepare(session, clients, llm, brand)
    review = session.query(Review).filter_by(store_review_id="gp-1").one()
    moderation.reject(session, review.active_reply.id, "tester", "слишком формально")
    session.commit()

    reasons = analytics.reject_reasons(session)
    assert reasons == [("слишком формально", 1)]
