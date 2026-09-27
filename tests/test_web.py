"""Проверка HTTP-слоя модерации на in-memory базе."""

from __future__ import annotations

import pytest

from app.models import Review
from app.pipeline import analyze, draft, ingest

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def client(session, clients, llm, brand):
    from app.web.api import app, get_db

    ingest.ingest(session, clients)
    analyze.analyze_sync(session, llm, brand, "fake-model")
    draft.generate_drafts(session, llm, brand, "fake-model", clients)
    session.commit()

    app.dependency_overrides[get_db] = lambda: session
    with fastapi_testclient.TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_queue_endpoint_returns_cards(client):
    payload = client.get("/api/queue").json()
    assert payload["items"]

    card = payload["items"][0]
    assert card["review"]["store"] in {"app_store", "google_play"}
    assert card["analysis"]["severity"] >= 1
    assert card["char_limit"] in {350, 5970}


def test_queue_is_sorted_by_severity(client):
    items = client.get("/api/queue").json()["items"]
    severities = [i["analysis"]["severity"] for i in items if i["analysis"]]
    assert severities == sorted(severities, reverse=True)


def test_approve_records_moderator(client, session):
    reply_id = client.get("/api/queue").json()["items"][-1]["reply_id"]
    response = client.post(
        f"/api/replies/{reply_id}/approve",
        json={"text": "Короткий понятный ответ."},
        headers={"X-Moderator": "irina"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    detail = client.get(f"/api/replies/{reply_id}").json()
    assert detail["moderator"] == "irina"
    assert detail["was_edited"] is True


def test_approve_over_limit_is_rejected(client):
    items = client.get("/api/queue").json()["items"]
    play = next(i for i in items if i["review"]["store"] == "google_play")

    response = client.post(
        f"/api/replies/{play['reply_id']}/approve", json={"text": "я" * 400}
    )
    assert response.status_code == 400
    assert "лимит" in response.json()["detail"].lower()


def test_dashboard_and_alerts_endpoints(client):
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["week"]["reviews"] == 4

    alerts = client.get("/api/alerts").json()["alerts"]
    assert any("legal_threat" in a["detail"] for a in alerts)
