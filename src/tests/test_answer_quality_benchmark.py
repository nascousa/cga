from pathlib import Path

import pytest

from scripts.run_answer_quality_benchmark import (
    AnswerCheckpointError,
    load_answer_checkpoint,
    render_markdown,
    run_answer_benchmark,
    write_answer_checkpoint,
)


class FakeRunner:
    def run_case(self, _case: dict, mode: str) -> dict:
        if mode == "baseline":
            return {
                "answer": "run is implemented in src/service.py.",
                "citations": ["file:src/service.py#run"],
                "promptSha256": "baseline-prompt",
                "usage": {"prompt_tokens": 100},
            }
        return {
            "answer": "run is implemented in src/service.py.",
            "citations": [],
            "promptSha256": "cg-prompt",
            "usage": {"prompt_tokens": 10},
        }


class AdaptiveRunner:
    def __init__(self) -> None:
        self.cg_chunk_counts: list[int] = []

    def run_case(self, case: dict, mode: str) -> dict:
        if mode == "baseline":
            return {
                "answer": "run in src/service.py calls helper.",
                "citations": ["file:src/service.py#run", "relation:CALLS:service.run->service.helper"],
                "promptSha256": "baseline-prompt",
            }
        chunk_count = len(case["cg"]["chunks"])
        self.cg_chunk_counts.append(chunk_count)
        if chunk_count == 1:
            return {
                "answer": "run is in src/service.py.",
                "citations": ["file:src/service.py#run"],
                "promptSha256": "initial-cg-prompt",
            }
        return {
            "answer": "run in src/service.py calls helper.",
            "citations": ["file:src/service.py#run", "relation:CALLS:service.run->service.helper"],
            "promptSha256": "expanded-cg-prompt",
        }


class InterruptingRunner(FakeRunner):
    def __init__(self, fail_case_id: str) -> None:
        self.fail_case_id = fail_case_id
        self.calls: list[tuple[str, str]] = []

    def run_case(self, case: dict, mode: str) -> dict:
        case_id = str(case["id"])
        self.calls.append((case_id, mode))
        if case_id == self.fail_case_id and mode == "baseline":
            raise RuntimeError("simulated interruption")
        return super().run_case(case, mode)


class InterruptingAdaptiveRunner(AdaptiveRunner):
    def __init__(self, fail_case_id: str) -> None:
        super().__init__()
        self.fail_case_id = fail_case_id

    def run_case(self, case: dict, mode: str) -> dict:
        if (
            str(case["id"]) == self.fail_case_id
            and mode == "cg"
            and len(case["cg"]["chunks"]) > 1
        ):
            raise RuntimeError("simulated adaptive interruption")
        return super().run_case(case, mode)


def _case() -> dict:
    return {
        "id": "repo-live-01",
        "project": "Repo",
        "query": "Where is run implemented?",
        "goldItems": ["file:src/service.py#run"],
        "requiredFacts": ["run", "src/service.py"],
        "targetSymbols": ["service.run"],
        "baseline": {
            "chunks": [
                {
                    "text": "def run(): pass",
                    "evidence": ["file:src/service.py#run"],
                    "symbols": ["service.run"],
                }
            ],
            "symbols": ["service.run"],
        },
        "cg": {
            "chunks": [
                {
                    "text": "def run(): pass",
                    "evidence": ["file:src/service.py#run"],
                    "symbols": ["service.run"],
                }
            ],
            "symbols": ["service.run"],
            "retrievalTrace": [
                {"depth": 0, "qualifiedName": "service.run", "relationshipType": "TARGET"}
            ],
        },
    }


def _adaptive_case() -> dict:
    case = _case()
    case["goldItems"] = [
        "file:src/service.py#run",
        "relation:CALLS:service.run->service.helper",
    ]
    case["requiredFacts"] = ["run", "src/service.py", "helper"]
    case["baseline"]["chunks"].append(
        {
            "id": "baseline-helper",
            "text": "def helper(): pass",
            "evidence": ["relation:CALLS:service.run->service.helper"],
            "symbols": ["service.helper"],
        }
    )
    case["cgExpansionPool"] = [
        {
            "requiredFact": "helper",
            "chunk": {
                "id": "graph-expansion-01",
                "text": "def helper(): pass",
                "evidence": ["relation:CALLS:service.run->service.helper"],
                "symbols": ["service.helper"],
            },
            "trace": {
                "depth": 1,
                "relationshipType": "CALLS",
                "sourceQualifiedName": "service.run",
                "targetQualifiedName": "service.helper",
                "sourceFile": "src/helper.py",
                "lineRange": [1, 1],
                "evidenceId": "relation:CALLS:service.run->service.helper",
            },
        }
    ]
    return case


def _case_with_id(case_id: str) -> dict:
    case = _case()
    case["id"] = case_id
    return case


def _adaptive_case_with_id(case_id: str) -> dict:
    case = _adaptive_case()
    case["id"] = case_id
    return case


def test_run_answer_benchmark_builds_gates_calibration_and_replay() -> None:
    report = run_answer_benchmark(
        cases=[_case()],
        runner=FakeRunner(),
        model_run={"model": "fake", "toolBudget": 0},
    )

    assert report["controls"]["toolBudget"] == 0
    assert report["regressionGates"]["allPassed"] is False
    assert report["adaptiveExpansionSummary"]["totalModelCalls"] == 2
    assert report["projectSummary"][0]["taskPassRateDeltaPercent"] == -100.0
    assert report["cases"][0]["failureClassification"] == "model_variance_or_reasoning"
    assert report["cases"][0]["retrievalReplay"]["status"] == "replayed"
    assert report["cases"][0]["adaptiveExpansionDecision"]["expand"] is True


def test_render_markdown_includes_failed_gate_and_calibration() -> None:
    report = run_answer_benchmark(
        cases=[_case()],
        runner=FakeRunner(),
        model_run={"model": "fake", "toolBudget": 0},
    )

    markdown = render_markdown(report)

    assert "# CGA Answer Quality Benchmark" in markdown
    assert "| Repo | 1 | 100.0% | 0.0%" in markdown
    assert "model_variance_or_reasoning" in markdown
    assert "HPS Calibration" in markdown
    assert "Adaptive Expansion" in markdown


def test_run_answer_benchmark_executes_frozen_graph_expansion() -> None:
    runner = AdaptiveRunner()

    report = run_answer_benchmark(
        cases=[_adaptive_case()],
        runner=runner,
        model_run={"model": "fake", "toolBudget": 0},
    )

    record = report["cases"][0]
    assert runner.cg_chunk_counts == [1, 2]
    assert record["cgInitial"]["taskPassed"] is False
    assert record["cg"]["taskPassed"] is True
    assert record["adaptiveExpansion"]["triggered"] is True
    assert record["adaptiveExpansion"]["stopReason"] == "answer_passed"
    assert record["adaptiveExpansion"]["attempts"][0]["depth"] == 1
    assert record["retrievalReplay"]["relationshipTypesVisited"] == ["CALLS", "TARGET"]
    assert report["adaptiveExpansionSummary"]["recoveredCases"] == 1
    assert report["adaptiveExpansionSummary"]["totalModelCalls"] == 3
    assert report["regressionGates"]["allPassed"] is True


def test_adaptive_expansion_honors_zero_chunk_budget_without_rerun() -> None:
    runner = AdaptiveRunner()

    report = run_answer_benchmark(
        cases=[_adaptive_case()],
        runner=runner,
        model_run={"model": "fake", "toolBudget": 0},
        max_expansion_chunks=0,
    )

    record = report["cases"][0]
    assert runner.cg_chunk_counts == [1]
    assert record["cg"]["taskPassed"] is False
    assert record["adaptiveExpansion"]["attempts"] == []
    assert record["adaptiveExpansion"]["stopReason"] == "chunk_budget_exhausted"
    assert report["adaptiveExpansionSummary"]["totalModelCalls"] == 2


def test_resume_skips_completed_initial_cases() -> None:
    cases = [_case_with_id("repo-live-01"), _case_with_id("repo-live-02")]
    checkpoints: list[dict] = []
    interrupted_runner = InterruptingRunner("repo-live-02")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        run_answer_benchmark(
            cases=cases,
            runner=interrupted_runner,
            model_run={"model": "fake", "toolBudget": 0},
            checkpoint_callback=checkpoints.append,
        )

    checkpoint = checkpoints[-1]
    assert checkpoint["phase"] == "initial"
    assert [item["id"] for item in checkpoint["initialRecords"]] == ["repo-live-01"]

    resumed_runner = InterruptingRunner("never")
    resumed_checkpoints: list[dict] = []
    report = run_answer_benchmark(
        cases=cases,
        runner=resumed_runner,
        model_run={"model": "fake", "toolBudget": 0},
        resume_state=checkpoint,
        checkpoint_callback=resumed_checkpoints.append,
    )

    assert resumed_runner.calls == [
        ("repo-live-02", "baseline"),
        ("repo-live-02", "cg"),
    ]
    assert report["execution"] == {
        "checkpointVersion": 1,
        "resumedInitialCases": 1,
        "resumedFinalCases": 0,
        "modelCallsExecutedNow": 2,
    }
    assert resumed_checkpoints
    assert all(item["initialRecords"] for item in resumed_checkpoints)


def test_fresh_run_clears_stale_checkpoint_before_first_request() -> None:
    checkpoints: list[dict] = [{"stale": True}]

    with pytest.raises(RuntimeError, match="simulated interruption"):
        run_answer_benchmark(
            cases=[_case()],
            runner=InterruptingRunner("repo-live-01"),
            model_run={"model": "fake", "toolBudget": 0},
            checkpoint_callback=checkpoints.append,
        )

    assert checkpoints[-1]["phase"] == "initial"
    assert checkpoints[-1]["initialRecords"] == []
    assert checkpoints[-1]["finalResults"] == []


def test_resume_skips_completed_adaptive_cases() -> None:
    cases = [
        _adaptive_case_with_id("repo-live-01"),
        _adaptive_case_with_id("repo-live-02"),
    ]
    checkpoints: list[dict] = []

    with pytest.raises(RuntimeError, match="simulated adaptive interruption"):
        run_answer_benchmark(
            cases=cases,
            runner=InterruptingAdaptiveRunner("repo-live-02"),
            model_run={"model": "fake", "toolBudget": 0},
            checkpoint_callback=checkpoints.append,
        )

    checkpoint = checkpoints[-1]
    assert checkpoint["phase"] == "adaptive"
    assert len(checkpoint["initialRecords"]) == 2
    assert [item["record"]["id"] for item in checkpoint["finalResults"]] == [
        "repo-live-01"
    ]

    resumed_runner = AdaptiveRunner()
    report = run_answer_benchmark(
        cases=cases,
        runner=resumed_runner,
        model_run={"model": "fake", "toolBudget": 0},
        resume_state=checkpoint,
    )

    assert resumed_runner.cg_chunk_counts == [2]
    assert report["execution"] == {
        "checkpointVersion": 1,
        "resumedInitialCases": 2,
        "resumedFinalCases": 1,
        "modelCallsExecutedNow": 1,
    }
    assert report["regressionGates"]["allPassed"] is True


def test_resume_rejects_different_model_identity() -> None:
    checkpoints: list[dict] = []
    run_answer_benchmark(
        cases=[_case()],
        runner=FakeRunner(),
        model_run={"model": "fake", "toolBudget": 0},
        checkpoint_callback=checkpoints.append,
    )

    with pytest.raises(AnswerCheckpointError, match="checkpoint identity"):
        run_answer_benchmark(
            cases=[_case()],
            runner=FakeRunner(),
            model_run={"model": "different", "toolBudget": 0},
            resume_state=checkpoints[-1],
        )


def test_checkpoint_round_trip_is_atomic(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "answer.checkpoint.json"
    state = {
        "version": 1,
        "identity": {"caseSet": {"sha256": "abc"}},
        "phase": "initial",
        "initialRecords": [],
        "finalResults": [],
    }

    write_answer_checkpoint(checkpoint_path, state)

    assert load_answer_checkpoint(checkpoint_path) == state
    assert not (tmp_path / ".answer.checkpoint.json.tmp").exists()