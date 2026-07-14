"""Answer-level quality scoring for baseline-vs-CGA benchmark runs."""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Any


ANSWER_SCORING_VERSION = "natural-facts-v3"

_INFLECTED_FACT_PATTERNS = {
    "call": r"call(?:s|ed|ing)?",
    "flow": r"(?:flow(?:s|ed|ing)?|flows_to)",
    "import": r"import(?:s|ed|ing)?",
    "return": r"return(?:s|ed|ing)?",
}


class AnswerQualityInputError(ValueError):
    """Raised when an answer-quality benchmark payload is invalid."""


def score_answer(
    *,
    case: dict[str, Any],
    mode: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    """Score one structured model answer against frozen facts and evidence IDs."""
    if mode not in {"baseline", "cg"}:
        raise AnswerQualityInputError("mode must be 'baseline' or 'cg'")
    context = case.get(mode)
    if not isinstance(context, dict):
        raise AnswerQualityInputError(f"case.{mode} must be an object")

    answer = str(response.get("answer") or "").strip()
    citations = _string_list(response.get("citations") or [], "response.citations")
    gold_items = _string_list(case.get("goldItems") or [], "case.goldItems")
    required_facts = _string_list(case.get("requiredFacts") or [], "case.requiredFacts")

    matched_facts = [fact for fact in required_facts if _answer_contains_fact(answer, fact)]
    fact_coverage = _ratio(len(matched_facts), len(required_facts))

    available_evidence = {
        evidence
        for chunk in context.get("chunks") or []
        if isinstance(chunk, dict)
        for evidence in _string_list(chunk.get("evidence") or [], f"case.{mode}.chunks.evidence")
    }
    gold_set = set(gold_items)
    cited_set = set(citations)
    grounded_citations = sorted(cited_set & available_evidence)
    unsupported_citations = sorted(cited_set - available_evidence)
    covered_citations = sorted(gold_set & cited_set & available_evidence)
    citation_coverage = _ratio(len(covered_citations), len(gold_set))
    citation_precision = _ratio(len(covered_citations), len(cited_set))
    citation_grounding = _ratio(len(grounded_citations), len(cited_set))

    available_gold = sorted(gold_set & available_evidence)
    context_evidence_coverage = _ratio(len(available_gold), len(gold_set))
    task_passed = (
        bool(answer)
        and fact_coverage == 1.0
        and citation_coverage == 1.0
        and citation_grounding == 1.0
    )

    return {
        "answer": answer,
        "citations": citations,
        "matchedFacts": matched_facts,
        "factCoverage": round(fact_coverage, 4),
        "coveredCitations": covered_citations,
        "citationCoverage": round(citation_coverage, 4),
        "citationPrecision": round(citation_precision, 4),
        "groundedCitations": grounded_citations,
        "unsupportedCitations": unsupported_citations,
        "citationGrounding": round(citation_grounding, 4),
        "availableGoldItems": available_gold,
        "contextEvidenceCoverage": round(context_evidence_coverage, 4),
        "taskPassed": task_passed,
    }


def _answer_contains_fact(answer: str, fact: str) -> bool:
    normalized_answer = _normalize(answer)
    normalized_fact = _normalize(fact)
    fact_pattern = _INFLECTED_FACT_PATTERNS.get(
        normalized_fact,
        re.escape(normalized_fact),
    )
    return re.search(
        rf"(?<![a-z0-9_])(?:{fact_pattern})(?![a-z0-9_])",
        normalized_answer,
    ) is not None


def summarize_answer_quality(
    records: list[dict[str, Any]],
    *,
    max_task_regression_percent: float = 0.0,
    max_citation_regression_percent: float = 0.0,
) -> list[dict[str, Any]]:
    """Build per-project quality deltas and enforce regression gates."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("project") or "unknown")].append(record)

    summaries: list[dict[str, Any]] = []
    for project in sorted(grouped):
        project_records = grouped[project]
        baseline_pass_rate = _average_bool(
            [bool(record["baseline"]["taskPassed"]) for record in project_records]
        )
        cg_pass_rate = _average_bool([bool(record["cg"]["taskPassed"]) for record in project_records])
        baseline_citation = _average(
            [float(record["baseline"]["citationCoverage"]) for record in project_records]
        )
        cg_citation = _average(
            [float(record["cg"]["citationCoverage"]) for record in project_records]
        )
        task_delta_percent = 100.0 * (cg_pass_rate - baseline_pass_rate)
        citation_delta_percent = 100.0 * (cg_citation - baseline_citation)
        task_gate_passed = task_delta_percent >= -max_task_regression_percent
        citation_gate_passed = citation_delta_percent >= -max_citation_regression_percent
        summaries.append(
            {
                "project": project,
                "caseCount": len(project_records),
                "baselineTaskPassRate": round(baseline_pass_rate, 4),
                "cgTaskPassRate": round(cg_pass_rate, 4),
                "taskPassRateDeltaPercent": round(task_delta_percent, 2),
                "baselineCitationCoverage": round(baseline_citation, 4),
                "cgCitationCoverage": round(cg_citation, 4),
                "citationCoverageDeltaPercent": round(citation_delta_percent, 2),
                "taskRegressionGatePassed": task_gate_passed,
                "citationRegressionGatePassed": citation_gate_passed,
                "regressionGatePassed": task_gate_passed and citation_gate_passed,
            }
        )
    return summaries


def calibrate_hps(records: list[dict[str, Any]], *, mode: str = "cg") -> dict[str, Any]:
    """Fit a deterministic HPS threshold to observed answer pass/fail outcomes."""
    if mode not in {"baseline", "cg"}:
        raise AnswerQualityInputError("mode must be 'baseline' or 'cg'")

    samples = [
        (
            str(record.get("id") or ""),
            float(record[mode]["hallucinationPressureScore"]),
            bool(record[mode]["taskPassed"]),
        )
        for record in records
    ]
    if not samples:
        return {
            "mode": mode,
            "sampleCount": 0,
            "bestThreshold": None,
            "balancedAccuracy": None,
            "unexpectedFailures": [],
            "unexpectedPasses": [],
        }

    thresholds = sorted({hps for _case_id, hps, _passed in samples})
    candidates = [thresholds[0] - 0.01, *thresholds]
    best_threshold = candidates[0]
    best_accuracy = -1.0
    best_confusion: dict[str, int] = {}
    for threshold in candidates:
        confusion = _confusion(samples, threshold)
        true_positive_rate = _ratio(confusion["predictedFailureAndFailed"], confusion["actualFailures"])
        true_negative_rate = _ratio(confusion["predictedPassAndPassed"], confusion["actualPasses"])
        balanced_accuracy = (true_positive_rate + true_negative_rate) / 2.0
        if balanced_accuracy > best_accuracy:
            best_threshold = threshold
            best_accuracy = balanced_accuracy
            best_confusion = confusion

    unexpected_failures = [
        case_id for case_id, hps, passed in samples if not passed and hps <= best_threshold
    ]
    unexpected_passes = [
        case_id for case_id, hps, passed in samples if passed and hps > best_threshold
    ]
    passing_hps = [hps for _case_id, hps, passed in samples if passed]
    failing_hps = [hps for _case_id, hps, passed in samples if not passed]
    return {
        "mode": mode,
        "sampleCount": len(samples),
        "bestThreshold": round(best_threshold, 2),
        "balancedAccuracy": round(best_accuracy, 4),
        "averagePassingHps": round(_average(passing_hps), 2) if passing_hps else None,
        "averageFailingHps": round(_average(failing_hps), 2) if failing_hps else None,
        "confusion": best_confusion,
        "unexpectedFailures": unexpected_failures,
        "unexpectedPasses": unexpected_passes,
    }


def adaptive_expansion_decision(
    *,
    hps: float,
    context_evidence_coverage: float,
    calibrated_threshold: float | None,
    answer_passed: bool | None = None,
    current_depth: int = 0,
    max_depth: int = 2,
) -> dict[str, Any]:
    """Decide whether the next graph hop is justified by risk or observed failure."""
    reasons: list[str] = []
    if context_evidence_coverage < 1.0:
        reasons.append("missing_evidence")
    if calibrated_threshold is not None and hps > calibrated_threshold:
        reasons.append("hps_above_calibrated_threshold")
    if answer_passed is False:
        reasons.append("answer_failed")
        if calibrated_threshold is not None and hps <= calibrated_threshold:
            reasons.append("hps_outcome_gap")

    depth_available = current_depth < max_depth
    return {
        "expand": bool(reasons) and depth_available,
        "reasons": reasons,
        "currentDepth": current_depth,
        "maxDepth": max_depth,
        "depthAvailable": depth_available,
    }


def classify_failure(record: dict[str, Any]) -> str | None:
    """Classify a CG answer failure using evidence coverage and baseline outcome."""
    cg = record["cg"]
    if cg["taskPassed"]:
        return None
    if float(cg["contextEvidenceCoverage"]) < 1.0:
        return "missing_retrieval_evidence"
    if bool(record["baseline"]["taskPassed"]):
        return "model_variance_or_reasoning"
    return "task_or_model_failure"


def _confusion(samples: list[tuple[str, float, bool]], threshold: float) -> dict[str, int]:
    actual_failures = sum(1 for _case_id, _hps, passed in samples if not passed)
    actual_passes = len(samples) - actual_failures
    return {
        "actualFailures": actual_failures,
        "actualPasses": actual_passes,
        "predictedFailureAndFailed": sum(
            1 for _case_id, hps, passed in samples if hps > threshold and not passed
        ),
        "predictedPassAndPassed": sum(
            1 for _case_id, hps, passed in samples if hps <= threshold and passed
        ),
    }


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AnswerQualityInputError(f"{label} must be a list of strings")
    return [item for item in value if item]


def _normalize(value: str) -> str:
    return " ".join(value.replace("\\", "/").lower().split())


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return numerator / denominator


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _average_bool(values: list[bool]) -> float:
    return _average([1.0 if value else 0.0 for value in values])