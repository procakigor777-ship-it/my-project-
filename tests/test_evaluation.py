"""Проверка самого скорера: он должен ловить расхождения, а не радовать отчётом."""

from __future__ import annotations

import json

from app import evaluation
from app.pipeline import ingest


def write_eval(tmp_path, cases):
    path = tmp_path / "eval.json"
    path.write_text(json.dumps({"cases": cases}, ensure_ascii=False), encoding="utf-8")
    return path


def test_correct_labels_score_high(session, clients, llm, brand, tmp_path):
    ingest.ingest(session, clients)
    path = write_eval(
        tmp_path,
        [
            {
                "review_id": "as-1",
                "sentiment": "negative",
                "severity_range": [4, 5],
                "topics_any_of": ["crash"],
                "is_actionable_bug": True,
                "risk_flags": [],
            },
            {
                "review_id": "as-3",
                "sentiment": "negative",
                "severity_range": [5, 5],
                "topics_any_of": ["other", "privacy"],
                "risk_flags": ["legal_threat"],
            },
        ],
    )

    report = evaluation.run_eval(session, llm, brand, "fake-model", path=path)

    assert report.cases == 2
    assert report.overall == 1.0
    assert not report.mismatches
    assert report.risk_false_positives == 0 and report.risk_false_negatives == 0


def test_wrong_label_is_reported(session, clients, llm, brand, tmp_path):
    ingest.ingest(session, clients)
    path = write_eval(
        tmp_path,
        [
            {
                "review_id": "as-1",
                "sentiment": "positive",  # заведомо неверно
                "severity_range": [1, 1],
                "topics_any_of": ["billing"],
                "risk_flags": [],
            }
        ],
    )

    report = evaluation.run_eval(session, llm, brand, "fake-model", path=path)

    assert report.overall < 1.0
    fields = {m.field for m in report.mismatches}
    assert {"sentiment", "severity_in_range", "topic_hit"} <= fields


def test_risk_false_positive_counted_separately(session, clients, llm, brand, tmp_path):
    """Лишний risk_flag — отдельная ошибка: человек разбирает поток безобидных отзывов."""
    ingest.ingest(session, clients)
    path = write_eval(
        tmp_path,
        [
            {
                "review_id": "as-3",
                "sentiment": "negative",
                "severity_range": [5, 5],
                "topics_any_of": ["other"],
                "risk_flags": [],  # модель поставит legal_threat
            }
        ],
    )

    report = evaluation.run_eval(session, llm, brand, "fake-model", path=path)

    assert report.risk_false_positives == 1
    assert report.risk_false_negatives == 0


def test_missing_review_is_not_silent(session, llm, brand, tmp_path):
    path = write_eval(
        tmp_path,
        [{"review_id": "нет-такого", "sentiment": "negative", "severity_range": [1, 5],
          "topics_any_of": [], "risk_flags": []}],
    )

    report = evaluation.run_eval(session, llm, brand, "fake-model", path=path)

    assert report.cases == 0
    assert report.missing == ["нет-такого"]


def test_shipped_eval_set_matches_fixtures():
    """Каждый кейс разметки должен ссылаться на существующий отзыв в фикстурах."""
    import pathlib

    cases = evaluation.load_cases(pathlib.Path("fixtures/eval_set.json"))
    fixture_ids = set()
    for name in ("app_store", "google_play"):
        data = json.loads(
            pathlib.Path(f"fixtures/{name}.json").read_text(encoding="utf-8")
        )
        fixture_ids |= {item["id"] for item in data}

    case_ids = {c["review_id"] for c in cases}
    assert case_ids <= fixture_ids, f"разметка без отзыва: {case_ids - fixture_ids}"
    assert fixture_ids == case_ids, f"отзыв без разметки: {fixture_ids - case_ids}"

    for case in cases:
        low, high = case["severity_range"]
        assert 1 <= low <= high <= 5, case["review_id"]
