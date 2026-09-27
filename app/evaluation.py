"""Проверка качества классификации на размеченном наборе.

Без этого любая правка промпта — гадание: прогон занимает минуты и
сразу показывает, стало лучше или хуже. Набор лежит в
`fixtures/eval_set.json`, разметка ручная.

Severity сравнивается диапазоном, а не точным значением: спор между
3 и 4 у двух живых людей — норма. Темы засчитываются по попаданию хотя
бы одной ожидаемой. А risk_flags сверяются множеством целиком: ложное
срабатывание здесь так же плохо, как пропуск — в первом случае человек
разбирает поток безобидных отзывов, во втором автоответчик пишет
юристу истца.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.analysis import analyze_review
from app.llm.brand import BrandProfile
from app.llm.client import LLMClient, LLMError
from app.llm.schemas import ReviewAnalysisResult
from app.models import Review

EVAL_SET_PATH = Path("fixtures/eval_set.json")

BOOL_FIELDS = ("is_actionable_bug", "requests_refund", "contains_pii", "mentions_competitor")


@dataclass
class Mismatch:
    review_id: str
    field: str
    expected: object
    actual: object

    def __str__(self) -> str:
        return f"{self.review_id}: {self.field} ожидалось {self.expected}, получено {self.actual}"


@dataclass
class Score:
    checked: int = 0
    passed: int = 0

    @property
    def rate(self) -> float:
        return self.passed / self.checked if self.checked else 0.0


@dataclass
class EvalReport:
    cases: int = 0
    scores: dict[str, Score] = field(default_factory=dict)
    mismatches: list[Mismatch] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    #: Ложные срабатывания и пропуски по risk_flags считаются отдельно:
    #: цена этих двух ошибок разная, усреднять их бессмысленно.
    risk_false_positives: int = 0
    risk_false_negatives: int = 0

    def record(self, name: str, review_id: str, ok: bool, expected, actual) -> None:
        score = self.scores.setdefault(name, Score())
        score.checked += 1
        if ok:
            score.passed += 1
        else:
            self.mismatches.append(Mismatch(review_id, name, expected, actual))

    def as_dict(self) -> dict:
        return {
            "cases": self.cases,
            "metrics": {
                name: {"checked": s.checked, "passed": s.passed, "rate": round(s.rate, 3)}
                for name, s in sorted(self.scores.items())
            },
            "risk_flags": {
                "false_positives": self.risk_false_positives,
                "false_negatives": self.risk_false_negatives,
            },
            "mismatches": [str(m) for m in self.mismatches],
            "missing_reviews": self.missing,
            "errors": self.errors,
        }

    @property
    def overall(self) -> float:
        total_checked = sum(s.checked for s in self.scores.values())
        total_passed = sum(s.passed for s in self.scores.values())
        return total_passed / total_checked if total_checked else 0.0


def load_cases(path: Path = EVAL_SET_PATH) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["cases"]


def compare(report: EvalReport, case: dict, result: ReviewAnalysisResult) -> None:
    rid = case["review_id"]

    report.record(
        "sentiment", rid, result.sentiment.value == case["sentiment"],
        case["sentiment"], result.sentiment.value,
    )

    low, high = case["severity_range"]
    report.record(
        "severity_in_range", rid, low <= result.severity <= high,
        f"{low}-{high}", result.severity,
    )

    expected_topics = set(case.get("topics_any_of", []))
    actual_topics = {t.value for t in result.topics}
    if expected_topics:
        report.record(
            "topic_hit", rid, bool(expected_topics & actual_topics),
            sorted(expected_topics), sorted(actual_topics),
        )

    expected_risks = set(case.get("risk_flags", []))
    actual_risks = {f.value for f in result.risk_flags}
    report.record("risk_flags_exact", rid, expected_risks == actual_risks,
                  sorted(expected_risks), sorted(actual_risks))
    report.risk_false_positives += len(actual_risks - expected_risks)
    report.risk_false_negatives += len(expected_risks - actual_risks)

    for name in BOOL_FIELDS:
        if name in case:
            actual = getattr(result, name)
            report.record(name, rid, actual == case[name], case[name], actual)

    if "language" in case:
        actual_language = (result.language or "").lower()[:2]
        report.record(
            "language", rid, actual_language == case["language"][:2],
            case["language"], result.language,
        )


def run_eval(
    session: Session,
    llm: LLMClient,
    brand: BrandProfile,
    model: str,
    path: Path = EVAL_SET_PATH,
    limit: int | None = None,
) -> EvalReport:
    """Гоняет анализ по размеченным отзывам и сравнивает с разметкой.

    Результаты не пишутся в БД: прогон оценивает промпт, а не наполняет
    рабочие данные. Отзывы должны быть уже собраны (`reviews ingest`
    на мок-адаптерах).
    """
    report = EvalReport()
    cases = load_cases(path)
    if limit:
        cases = cases[:limit]

    for case in cases:
        review = session.execute(
            select(Review).where(Review.store_review_id == case["review_id"])
        ).scalar_one_or_none()
        if review is None:
            report.missing.append(case["review_id"])
            continue

        try:
            result = analyze_review(llm, brand, model, review)
        except LLMError as exc:
            report.errors.append(f"{case['review_id']}: {exc}")
            continue

        report.cases += 1
        compare(report, case, result)

    return report


def render(report: EvalReport) -> str:
    lines = [f"Проверено кейсов: {report.cases}", ""]
    for name, score in sorted(report.scores.items()):
        lines.append(f"  {name:<22} {score.passed:>3}/{score.checked:<3} {score.rate:>6.1%}")
    lines += [
        "",
        f"  risk_flags: ложных срабатываний {report.risk_false_positives}, "
        f"пропусков {report.risk_false_negatives}",
        f"  средняя точность: {report.overall:.1%}",
    ]
    if report.missing:
        lines.append(f"\nНет в базе (нужен ingest): {', '.join(report.missing)}")
    if report.errors:
        lines.append("\nОшибки вызовов:\n  " + "\n  ".join(report.errors))
    if report.mismatches:
        lines.append("\nРасхождения:")
        lines += [f"  {m}" for m in report.mismatches]
    return "\n".join(lines)
