from backend.perf.answer_quality import (
    adaptive_expansion_decision,
    calibrate_hps,
    classify_failure,
    score_answer,
    summarize_answer_quality,
)


def _case() -> dict:
    return {
        "id": "repo-live-01",
        "project": "Repo",
        "goldItems": ["file:src/service.py#run"],
        "requiredFacts": ["run", "src/service.py"],
        "baseline": {
            "chunks": [
                {
                    "text": "def run(): pass",
                    "evidence": ["file:src/service.py#run"],
                }
            ]
        },
        "cg": {
            "chunks": [
                {
                    "text": "def run(): pass",
                    "evidence": ["file:src/service.py#run"],
                }
            ]
        },
    }


def test_score_answer_requires_facts_and_gold_citations() -> None:
    result = score_answer(
        case=_case(),
        mode="cg",
        response={
            "answer": "run is implemented in src/service.py.",
            "citations": ["file:src/service.py#run"],
        },
    )

    assert result == {
        "answer": "run is implemented in src/service.py.",
        "citations": ["file:src/service.py#run"],
        "matchedFacts": ["run", "src/service.py"],
        "factCoverage": 1.0,
        "coveredCitations": ["file:src/service.py#run"],
        "citationCoverage": 1.0,
        "citationPrecision": 1.0,
        "groundedCitations": ["file:src/service.py#run"],
        "unsupportedCitations": [],
        "citationGrounding": 1.0,
        "availableGoldItems": ["file:src/service.py#run"],
        "contextEvidenceCoverage": 1.0,
        "taskPassed": True,
    }


def test_score_answer_rejects_guessed_gold_citation_missing_from_context() -> None:
    case = _case()
    case["cg"]["chunks"][0]["evidence"] = []

    result = score_answer(
        case=case,
        mode="cg",
        response={
            "answer": "run is implemented in src/service.py.",
            "citations": ["file:src/service.py#run"],
        },
    )

    assert result["citationCoverage"] == 0.0
    assert result["citationGrounding"] == 0.0
    assert result["unsupportedCitations"] == ["file:src/service.py#run"]
    assert result["taskPassed"] is False


def test_score_answer_matches_fact_boundaries_and_natural_verb_forms() -> None:
    case = _case()
    case["requiredFacts"] = ["run", "flow", "service.py"]

    false_positive_result = score_answer(
        case=case,
        mode="cg",
        response={
            "answer": "WorkflowFailure uses runner.py from service.py.",
            "citations": ["file:src/service.py#run"],
        },
    )
    natural_form_result = score_answer(
        case=case,
        mode="cg",
        response={
            "answer": "run flows through service.py.",
            "citations": ["file:src/service.py#run"],
        },
    )
    canonical_form_result = score_answer(
        case=case,
        mode="cg",
        response={
            "answer": "run has a FLOWS_TO relation in service.py.",
            "citations": ["file:src/service.py#run"],
        },
    )

    assert false_positive_result["matchedFacts"] == ["service.py"]
    assert false_positive_result["factCoverage"] == 0.3333
    assert false_positive_result["taskPassed"] is False
    assert natural_form_result["matchedFacts"] == ["run", "flow", "service.py"]
    assert natural_form_result["taskPassed"] is True
    assert canonical_form_result["matchedFacts"] == ["run", "flow", "service.py"]
    assert canonical_form_result["taskPassed"] is True


def test_project_regression_gate_detects_cg_failure() -> None:
    records = [
        {
            "project": "Repo",
            "baseline": {"taskPassed": True, "citationCoverage": 1.0},
            "cg": {"taskPassed": False, "citationCoverage": 0.0},
        }
    ]

    assert summarize_answer_quality(records) == [
        {
            "project": "Repo",
            "caseCount": 1,
            "baselineTaskPassRate": 1.0,
            "cgTaskPassRate": 0.0,
            "taskPassRateDeltaPercent": -100.0,
            "baselineCitationCoverage": 1.0,
            "cgCitationCoverage": 0.0,
            "citationCoverageDeltaPercent": -100.0,
            "taskRegressionGatePassed": False,
            "citationRegressionGatePassed": False,
            "regressionGatePassed": False,
        }
    ]


def test_hps_calibration_surfaces_outcome_gap() -> None:
    records = [
        {
            "id": "low-pass",
            "cg": {"hallucinationPressureScore": 10, "taskPassed": True},
        },
        {
            "id": "low-fail",
            "cg": {"hallucinationPressureScore": 10, "taskPassed": False},
        },
        {
            "id": "high-fail",
            "cg": {"hallucinationPressureScore": 30, "taskPassed": False},
        },
    ]

    calibration = calibrate_hps(records)

    assert calibration["bestThreshold"] == 10
    assert calibration["unexpectedFailures"] == ["low-fail"]
    assert calibration["unexpectedPasses"] == []


def test_adaptive_expansion_uses_hps_outcome_gap_and_depth_cap() -> None:
    decision = adaptive_expansion_decision(
        hps=8,
        context_evidence_coverage=1.0,
        calibrated_threshold=10,
        answer_passed=False,
        current_depth=0,
        max_depth=2,
    )

    assert decision["expand"] is True
    assert decision["reasons"] == ["answer_failed", "hps_outcome_gap"]

    capped = adaptive_expansion_decision(
        hps=20,
        context_evidence_coverage=0.5,
        calibrated_threshold=10,
        current_depth=2,
        max_depth=2,
    )
    assert capped["expand"] is False
    assert capped["depthAvailable"] is False


def test_failure_classification_separates_missing_evidence_from_model_variance() -> None:
    missing = {
        "baseline": {"taskPassed": True},
        "cg": {"taskPassed": False, "contextEvidenceCoverage": 0.5},
    }
    complete = {
        "baseline": {"taskPassed": True},
        "cg": {"taskPassed": False, "contextEvidenceCoverage": 1.0},
    }

    assert classify_failure(missing) == "missing_retrieval_evidence"
    assert classify_failure(complete) == "model_variance_or_reasoning"