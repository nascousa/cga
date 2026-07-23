#!/usr/bin/env python
"""Run controlled answer-level evaluation over a frozen CGA case set."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from backend.perf.answer_quality import (  # noqa: E402
    ANSWER_SCORING_VERSION,
    adaptive_expansion_decision,
    calibrate_hps,
    classify_failure,
    score_answer,
    summarize_answer_quality,
)
from backend.perf.answer_runner import (  # noqa: E402
    ModelRunConfig,
    OpenAICompatibleAnswerRunner,
)
from backend.perf.context_quality import benchmark_context_quality  # noqa: E402
from scripts.run_live_context_quality_benchmark import (  # noqa: E402
    DEFAULT_EXPANSION_CHUNK_BUDGET,
    frozen_case_set_sha256,
    load_frozen_cases,
)


class AnswerRunner(Protocol):
    """Small runner boundary used by the CLI and deterministic tests."""

    def run_case(self, case: dict[str, Any], mode: str) -> dict[str, Any]: ...


CHECKPOINT_VERSION = 1


class AnswerCheckpointError(ValueError):
    """Raised when an answer benchmark checkpoint cannot be safely resumed."""


def run_adaptive_expansion(
    *,
    case: dict[str, Any],
    initial_record: dict[str, Any],
    runner: AnswerRunner,
    calibrated_threshold: float | None,
    max_depth: int,
    max_expansion_chunks: int,
    max_context_tokens: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Add frozen graph hops by depth and rerun CG within explicit budgets."""
    working_case = copy.deepcopy(case)
    initial_score = copy.deepcopy(initial_record["cg"])
    initial_decision = adaptive_expansion_decision(
        hps=float(initial_score["hallucinationPressureScore"]),
        context_evidence_coverage=float(initial_score["contextEvidenceCoverage"]),
        calibrated_threshold=calibrated_threshold,
        answer_passed=bool(initial_score["taskPassed"]),
        current_depth=0,
        max_depth=max_depth,
    )
    expansion = {
        "triggered": bool(initial_decision["expand"]),
        "initialDecision": initial_decision,
        "budgets": {
            "maxDepth": max_depth,
            "maxExpansionChunks": max_expansion_chunks,
            "maxContextTokens": max_context_tokens,
        },
        "attempts": [],
        "skippedCandidates": [],
        "addedChunkCount": 0,
        "finalDepth": 0,
        "stopReason": "not_triggered",
    }
    if not initial_decision["expand"]:
        return initial_score, working_case, expansion

    candidates = _ordered_expansion_candidates(case.get("cgExpansionPool"), max_depth=max_depth)
    if not candidates:
        expansion["stopReason"] = "no_expansion_candidates"
        return initial_score, working_case, expansion

    final_score = initial_score
    added_count = 0
    attempted_depths: set[int] = set()
    for depth in sorted({item["depth"] for item in candidates}):
        attempted_depths.add(depth)
        accepted: list[dict[str, Any]] = []
        context_score: dict[str, Any] | None = None
        for item in candidates:
            if item["depth"] != depth:
                continue
            if added_count >= max_expansion_chunks:
                expansion["skippedCandidates"].append(
                    {**item["trace"], "reason": "chunk_budget_exhausted"}
                )
                continue

            trial_case = copy.deepcopy(working_case)
            trial_case["cg"].setdefault("chunks", []).append(copy.deepcopy(item["chunk"]))
            trial_case["cg"].setdefault("symbols", []).extend(item["chunk"].get("symbols") or [])
            trial_case["cg"].setdefault("retrievalTrace", []).append(copy.deepcopy(item["trace"]))
            trial_context_score = _score_cg_context(trial_case)
            if int(trial_context_score["totalTokens"]) > max_context_tokens:
                expansion["skippedCandidates"].append(
                    {**item["trace"], "reason": "token_budget_exhausted"}
                )
                continue

            working_case = trial_case
            context_score = trial_context_score
            accepted.append(item)
            added_count += 1

        if not accepted:
            continue

        cg_response = runner.run_case(working_case, "cg")
        final_score = score_answer(case=working_case, mode="cg", response=cg_response)
        _add_run_metadata(final_score, context_score or _score_cg_context(working_case), cg_response)
        attempt_record = {
            "attempt": len(expansion["attempts"]) + 1,
            "depth": depth,
            "addedCandidates": [item["trace"] for item in accepted],
            "context": _context_snapshot(working_case, context_score or {}),
            "answerScore": final_score,
        }
        attempt_record["decision"] = adaptive_expansion_decision(
            hps=float(final_score["hallucinationPressureScore"]),
            context_evidence_coverage=float(final_score["contextEvidenceCoverage"]),
            calibrated_threshold=calibrated_threshold,
            answer_passed=bool(final_score["taskPassed"]),
            current_depth=depth,
            max_depth=max_depth,
        )
        expansion["attempts"].append(attempt_record)
        expansion["finalDepth"] = depth
        if final_score["taskPassed"]:
            expansion["stopReason"] = "answer_passed"
            break
        if added_count >= max_expansion_chunks:
            expansion["stopReason"] = "chunk_budget_exhausted"
            break
    else:
        expansion["stopReason"] = _expansion_exhaustion_reason(
            candidates=candidates,
            attempted_depths=attempted_depths,
            skipped_candidates=expansion["skippedCandidates"],
            max_depth=max_depth,
        )

    expansion["addedChunkCount"] = added_count
    if expansion["attempts"] and expansion["stopReason"] == "not_triggered":
        expansion["stopReason"] = "candidate_pool_exhausted"
    return final_score, working_case, expansion


def _ordered_expansion_candidates(value: Any, *, max_depth: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    candidates: list[dict[str, Any]] = []
    for index, candidate in enumerate(value):
        if not isinstance(candidate, dict):
            continue
        chunk = candidate.get("chunk")
        trace = candidate.get("trace")
        if not isinstance(chunk, dict) or not isinstance(trace, dict):
            continue
        try:
            depth = int(trace.get("depth"))
        except (TypeError, ValueError):
            continue
        if depth < 1 or depth > max_depth:
            continue
        candidates.append({"chunk": chunk, "trace": trace, "depth": depth, "index": index})
    priority = {"CALLS": 0, "IMPORTS": 1, "FLOWS_TO": 2}
    return sorted(
        candidates,
        key=lambda item: (
            item["depth"],
            priority.get(str(item["trace"].get("relationshipType")), 99),
            item["index"],
        ),
    )


def _score_cg_context(case: dict[str, Any]) -> dict[str, Any]:
    report = benchmark_context_quality(payload={"cases": [case]}, repo_root=Path.cwd())
    return report["cases"][0]["cg"]


def _add_run_metadata(
    answer_score: dict[str, Any],
    context_score: dict[str, Any],
    response: dict[str, Any],
) -> None:
    answer_score.update(
        {
            "hallucinationPressureScore": context_score["hallucinationPressureScore"],
            "contextTokens": context_score["totalTokens"],
            "promptSha256": response.get("promptSha256"),
            "usage": response.get("usage") or {},
            "requestAttempts": response.get("requestAttempts", 1),
        }
    )


def _context_snapshot(case: dict[str, Any], context_score: dict[str, Any]) -> dict[str, Any]:
    chunks = [item for item in case.get("cg", {}).get("chunks") or [] if isinstance(item, dict)]
    canonical = json.dumps(chunks, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "chunkCount": len(chunks),
        "chunkIds": [str(item.get("id") or "") for item in chunks],
        "evidenceIds": sorted(
            {
                str(evidence)
                for item in chunks
                for evidence in item.get("evidence") or []
                if evidence
            }
        ),
        "totalTokens": context_score.get("totalTokens"),
        "hallucinationPressureScore": context_score.get("hallucinationPressureScore"),
    }


def _expansion_exhaustion_reason(
    *,
    candidates: list[dict[str, Any]],
    attempted_depths: set[int],
    skipped_candidates: list[dict[str, Any]],
    max_depth: int,
) -> str:
    skipped_reasons = {str(item.get("reason")) for item in skipped_candidates}
    if skipped_reasons == {"token_budget_exhausted"}:
        return "token_budget_exhausted"
    if "chunk_budget_exhausted" in skipped_reasons:
        return "chunk_budget_exhausted"
    if attempted_depths and max(attempted_depths) >= max_depth:
        return "max_depth_reached"
    if candidates:
        return "candidate_pool_exhausted"
    return "no_expansion_candidates"


def replay_retrieval_trace(case: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Replay frozen retrieval steps and relate missing evidence to answer failure."""
    trace = case.get("cg", {}).get("retrievalTrace") or []
    gold_items = set(case.get("goldItems") or [])
    available_items = set(record["cg"].get("availableGoldItems") or [])
    missing_items = sorted(gold_items - available_items)
    relationship_types = sorted(
        {
            str(step.get("relationshipType"))
            for step in trace
            if isinstance(step, dict) and step.get("relationshipType")
        }
    )
    return {
        "status": "replayed" if trace else "trace_unavailable",
        "steps": trace,
        "relationshipTypesVisited": relationship_types,
        "missingGoldItems": missing_items,
        "queryDiagnostics": case.get("expansionQueryDiagnostics") or [],
        "diagnosis": record.get("failureClassification"),
    }


def summarize_adaptive_expansion(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize trigger, recovery, rerun, and stop-reason counts."""
    triggered = [record for record in records if record["adaptiveExpansion"]["triggered"]]
    stop_reasons = Counter(
        str(record["adaptiveExpansion"]["stopReason"])
        for record in records
    )
    return {
        "caseCount": len(records),
        "triggeredCases": len(triggered),
        "recoveredCases": sum(
            not record["cgInitial"]["taskPassed"] and record["cg"]["taskPassed"]
            for record in records
        ),
        "stillFailingCases": sum(not record["cg"]["taskPassed"] for record in records),
        "totalExpansionReruns": sum(
            len(record["adaptiveExpansion"]["attempts"])
            for record in records
        ),
        "totalModelCalls": 2 * len(records)
        + sum(
            len(record["adaptiveExpansion"]["attempts"])
            for record in records
        ),
        "stopReasons": dict(sorted(stop_reasons.items())),
    }


def run_answer_benchmark(
    *,
    cases: list[dict[str, Any]],
    runner: AnswerRunner,
    model_run: dict[str, Any],
    max_task_regression_percent: float = 0.0,
    max_citation_regression_percent: float = 0.0,
    max_cases: int = 0,
    max_expansion_depth: int = 2,
    max_expansion_chunks: int = DEFAULT_EXPANSION_CHUNK_BUDGET,
    max_cg_context_tokens: int = 4_000,
    resume_state: dict[str, Any] | None = None,
    checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run paired baseline/CG answers and build gates, calibration, and traces."""
    selected_cases = cases[:max_cases] if max_cases > 0 else cases
    case_set = {
        "caseCount": len(selected_cases),
        "sha256": frozen_case_set_sha256(selected_cases),
    }
    checkpoint_identity = {
        "caseSet": case_set,
        "answerScoringVersion": ANSWER_SCORING_VERSION,
        "modelRun": copy.deepcopy(model_run),
        "benchmarkControls": {
            "maxTaskRegressionPercent": max_task_regression_percent,
            "maxCitationRegressionPercent": max_citation_regression_percent,
            "maxExpansionDepth": max_expansion_depth,
            "maxExpansionChunks": max_expansion_chunks,
            "maxCgContextTokens": max_cg_context_tokens,
        },
    }
    case_ids = [str(case.get("id") or "") for case in selected_cases]
    if not all(case_ids) or len(case_ids) != len(set(case_ids)):
        raise AnswerCheckpointError("selected cases must have unique, non-empty ids")
    resumed_initial, resumed_final = _validate_resume_state(
        resume_state,
        identity=checkpoint_identity,
        case_ids=case_ids,
    )

    hps_report = benchmark_context_quality(
        payload={"cases": selected_cases},
        repo_root=Path.cwd(),
    )
    hps_by_id = {str(item.get("id") or ""): item for item in hps_report["cases"]}

    initial_records: list[dict[str, Any]] = []
    final_results: list[dict[str, Any]] = []
    model_calls_executed_now = 0
    if resume_state is None:
        _emit_checkpoint(
            checkpoint_callback,
            identity=checkpoint_identity,
            phase="initial",
            initial_records=initial_records,
            final_results=final_results,
        )
    for case in selected_cases:
        case_id = str(case.get("id") or "")
        if case_id in resumed_initial:
            initial_records.append(copy.deepcopy(resumed_initial[case_id]))
            continue
        hps_case = hps_by_id[case_id]
        baseline_response = runner.run_case(case, "baseline")
        cg_response = runner.run_case(case, "cg")
        model_calls_executed_now += 2
        baseline_score = score_answer(case=case, mode="baseline", response=baseline_response)
        cg_score = score_answer(case=case, mode="cg", response=cg_response)
        _add_run_metadata(baseline_score, hps_case["baseline"], baseline_response)
        _add_run_metadata(cg_score, hps_case["cg"], cg_response)
        record = {
            "id": case_id,
            "project": str(case.get("project") or ""),
            "query": str(case.get("query") or ""),
            "baseline": baseline_score,
            "cg": cg_score,
        }
        initial_records.append(record)
        _emit_checkpoint(
            checkpoint_callback,
            identity=checkpoint_identity,
            phase="initial",
            initial_records=initial_records,
            final_results=final_results,
        )

    initial_calibration = calibrate_hps(initial_records, mode="cg")
    threshold = initial_calibration.get("bestThreshold")
    calibrated_threshold = float(threshold) if threshold is not None else None
    final_cases: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for case, initial_record in zip(selected_cases, initial_records, strict=True):
        case_id = str(case.get("id") or "")
        if case_id in resumed_final:
            resumed_result = resumed_final[case_id]
            record = copy.deepcopy(resumed_result["record"])
            final_case = copy.deepcopy(resumed_result["case"])
            records.append(record)
            final_cases.append(final_case)
            final_results.append({"record": record, "case": final_case})
            continue

        record = copy.deepcopy(initial_record)
        record["cgInitial"] = copy.deepcopy(record["cg"])
        record["initialFailureClassification"] = classify_failure(record)
        final_score, final_case, expansion = run_adaptive_expansion(
            case=case,
            initial_record=record,
            runner=runner,
            calibrated_threshold=calibrated_threshold,
            max_depth=max_expansion_depth,
            max_expansion_chunks=max_expansion_chunks,
            max_context_tokens=max_cg_context_tokens,
        )
        model_calls_executed_now += len(expansion["attempts"])
        record["cg"] = final_score
        record["adaptiveExpansionDecision"] = expansion["initialDecision"]
        record["adaptiveExpansion"] = expansion
        record["failureClassification"] = classify_failure(record)
        record["retrievalReplay"] = replay_retrieval_trace(final_case, record)
        records.append(record)
        final_cases.append(final_case)
        final_results.append({"record": record, "case": final_case})
        _emit_checkpoint(
            checkpoint_callback,
            identity=checkpoint_identity,
            phase="adaptive",
            initial_records=initial_records,
            final_results=final_results,
        )

    project_summary = summarize_answer_quality(
        records,
        max_task_regression_percent=max_task_regression_percent,
        max_citation_regression_percent=max_citation_regression_percent,
    )
    final_calibration = calibrate_hps(records, mode="cg")
    final_context_report = benchmark_context_quality(
        payload={"cases": final_cases},
        repo_root=Path.cwd(),
    )

    report = {
        "method": "answer-quality-v3",
        "caseSet": case_set,
        "controls": {
            "answerScoringVersion": ANSWER_SCORING_VERSION,
            "pairedModes": ["baseline", "cg"],
            "sameModel": True,
            "samePromptTemplate": True,
            "toolBudget": 0,
            "adaptiveExpansion": {
                "calibrationSource": "initial-cg-runs",
                "maxDepth": max_expansion_depth,
                "maxExpansionChunks": max_expansion_chunks,
                "maxContextTokens": max_cg_context_tokens,
            },
        },
        "modelRun": model_run,
        "execution": {
            "checkpointVersion": CHECKPOINT_VERSION,
            "resumedInitialCases": len(resumed_initial),
            "resumedFinalCases": len(resumed_final),
            "modelCallsExecutedNow": model_calls_executed_now,
        },
        "regressionGates": {
            "maxTaskRegressionPercent": max_task_regression_percent,
            "maxCitationRegressionPercent": max_citation_regression_percent,
            "allPassed": all(item["regressionGatePassed"] for item in project_summary),
        },
        "projectSummary": project_summary,
        "adaptiveExpansionSummary": summarize_adaptive_expansion(records),
        "hpsCalibration": initial_calibration,
        "postExpansionHpsCalibration": final_calibration,
        "initialContextSummary": hps_report["summary"],
        "contextSummary": final_context_report["summary"],
        "cases": records,
    }
    _emit_checkpoint(
        checkpoint_callback,
        identity=checkpoint_identity,
        phase="complete",
        initial_records=initial_records,
        final_results=final_results,
    )
    return report


def _validate_resume_state(
    state: dict[str, Any] | None,
    *,
    identity: dict[str, Any],
    case_ids: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if state is None:
        return {}, {}
    if state.get("version") != CHECKPOINT_VERSION:
        raise AnswerCheckpointError(
            f"checkpoint version must be {CHECKPOINT_VERSION}"
        )
    if state.get("identity") != identity:
        raise AnswerCheckpointError(
            "checkpoint identity does not match the case set, model, or benchmark controls"
        )

    allowed_case_ids = set(case_ids)
    initial_records = _index_checkpoint_records(
        state.get("initialRecords"),
        label="initialRecords",
        case_ids=allowed_case_ids,
    )
    if list(initial_records) != case_ids[: len(initial_records)]:
        raise AnswerCheckpointError("checkpoint initialRecords must be a case-order prefix")

    final_results: dict[str, dict[str, Any]] = {}
    raw_final_results = state.get("finalResults")
    if not isinstance(raw_final_results, list):
        raise AnswerCheckpointError("checkpoint finalResults must be an array")
    for item in raw_final_results:
        if not isinstance(item, dict):
            raise AnswerCheckpointError("checkpoint finalResults entries must be objects")
        record = item.get("record")
        case = item.get("case")
        if not isinstance(record, dict) or not isinstance(case, dict):
            raise AnswerCheckpointError(
                "checkpoint finalResults entries must contain record and case objects"
            )
        case_id = str(record.get("id") or "")
        if case_id not in allowed_case_ids or str(case.get("id") or "") != case_id:
            raise AnswerCheckpointError(
                f"checkpoint final result has invalid case id: {case_id or '<empty>'}"
            )
        if case_id not in initial_records:
            raise AnswerCheckpointError(
                f"checkpoint final result is missing its initial record: {case_id}"
            )
        if case_id in final_results:
            raise AnswerCheckpointError(
                f"checkpoint contains duplicate final result: {case_id}"
            )
        final_results[case_id] = item
    if list(final_results) != case_ids[: len(final_results)]:
        raise AnswerCheckpointError("checkpoint finalResults must be a case-order prefix")
    return initial_records, final_results


def _index_checkpoint_records(
    value: Any,
    *,
    label: str,
    case_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise AnswerCheckpointError(f"checkpoint {label} must be an array")
    records: dict[str, dict[str, Any]] = {}
    for record in value:
        if not isinstance(record, dict):
            raise AnswerCheckpointError(f"checkpoint {label} entries must be objects")
        case_id = str(record.get("id") or "")
        if case_id not in case_ids:
            raise AnswerCheckpointError(
                f"checkpoint {label} has invalid case id: {case_id or '<empty>'}"
            )
        if case_id in records:
            raise AnswerCheckpointError(
                f"checkpoint {label} contains duplicate case id: {case_id}"
            )
        records[case_id] = record
    return records


def _emit_checkpoint(
    callback: Callable[[dict[str, Any]], None] | None,
    *,
    identity: dict[str, Any],
    phase: str,
    initial_records: list[dict[str, Any]],
    final_results: list[dict[str, Any]],
) -> None:
    if callback is None:
        return
    callback(
        {
            "version": CHECKPOINT_VERSION,
            "identity": copy.deepcopy(identity),
            "phase": phase,
            "initialRecords": copy.deepcopy(initial_records),
            "finalResults": copy.deepcopy(final_results),
        }
    )


def render_markdown(report: dict[str, Any]) -> str:
    """Render answer-level outcomes and regression gates."""
    lines = [
        "# CGA Answer Quality Benchmark",
        "",
        f"Method: `{report['method']}`",
        f"Frozen case set: `{report['caseSet']['sha256']}` ({report['caseSet']['caseCount']} cases)",
        f"Model: `{report['modelRun'].get('model', '')}`",
        f"Tool budget: {report['controls']['toolBudget']}",
        "",
        "## Regression Gates",
        "",
        "| Project | Cases | Baseline Pass | CG Pass | Pass Delta | Baseline Citations | CG Citations | Citation Delta | Gate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in report["projectSummary"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    item["project"],
                    str(item["caseCount"]),
                    _percent(item["baselineTaskPassRate"]),
                    _percent(item["cgTaskPassRate"]),
                    f"{item['taskPassRateDeltaPercent']}%",
                    _percent(item["baselineCitationCoverage"]),
                    _percent(item["cgCitationCoverage"]),
                    f"{item['citationCoverageDeltaPercent']}%",
                    "PASS" if item["regressionGatePassed"] else "FAIL",
                ]
            )
            + " |"
        )

    calibration = report["hpsCalibration"]
    expansion = report["adaptiveExpansionSummary"]
    lines.extend(
        [
            "",
            "## Adaptive Expansion",
            "",
            f"- Triggered cases: {expansion['triggeredCases']}",
            f"- Recovered initial failures: {expansion['recoveredCases']}",
            f"- Expansion reruns: {expansion['totalExpansionReruns']}",
            f"- Total model calls: {expansion['totalModelCalls']}",
            f"- Stop reasons: `{json.dumps(expansion['stopReasons'], sort_keys=True)}`",
            "",
            "## HPS Calibration",
            "",
            f"- Threshold: {calibration.get('bestThreshold')}",
            f"- Balanced accuracy: {calibration.get('balancedAccuracy')}",
            f"- Low-HPS failures: {len(calibration.get('unexpectedFailures') or [])}",
            f"- High-HPS passes: {len(calibration.get('unexpectedPasses') or [])}",
            "",
            "## Failed CG Cases",
            "",
        ]
    )
    failed = [item for item in report["cases"] if not item["cg"]["taskPassed"]]
    if not failed:
        lines.append("No CG answer failures.")
    else:
        lines.extend(
            [
                "| Case | Project | Diagnosis | HPS | Evidence Coverage | Expansion Reasons |",
                "|---|---|---|---:|---:|---|",
            ]
        )
        for item in failed:
            lines.append(
                "| "
                + " | ".join(
                    [
                        item["id"],
                        item["project"],
                        str(item["failureClassification"]),
                        str(item["cg"]["hallucinationPressureScore"]),
                        _percent(item["cg"]["contextEvidenceCoverage"]),
                        ", ".join(item["adaptiveExpansionDecision"]["reasons"]),
                    ]
                )
                + " |"
            )
    lines.append("")
    return "\n".join(lines)


def _percent(value: float) -> str:
    return f"{round(100.0 * value, 2)}%"


def load_answer_checkpoint(path: Path) -> dict[str, Any]:
    """Load a checkpoint object from disk."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnswerCheckpointError(f"could not load checkpoint {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AnswerCheckpointError(f"checkpoint {path} must contain a JSON object")
    return payload


def write_answer_checkpoint(path: Path, state: dict[str, Any]) -> None:
    """Atomically replace a checkpoint after a complete case result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary_path.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run CGA answer-level quality benchmarks")
    parser.add_argument("--input", required=True, help="Frozen JSONL case manifest")
    parser.add_argument("--output", default="answer-quality-report.json")
    parser.add_argument("--markdown", default="answer-quality-report.md")
    parser.add_argument("--model", default=os.getenv("CGA_EVAL_MODEL", ""))
    parser.add_argument("--base-url", default=os.getenv("CGA_EVAL_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--api-key-env", default="CGA_EVAL_API_KEY")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--requests-per-minute",
        type=float,
        default=0.0,
        help="Maximum HTTP requests per minute; 0 disables throttling",
    )
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-backoff-seconds", type=float, default=1.0)
    parser.add_argument("--max-retry-delay-seconds", type=float, default=60.0)
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--max-expansion-depth", type=int, default=2)
    parser.add_argument(
        "--max-expansion-chunks",
        type=int,
        default=DEFAULT_EXPANSION_CHUNK_BUDGET,
    )
    parser.add_argument("--max-cg-context-tokens", type=int, default=4_000)
    parser.add_argument("--max-task-regression-percent", type=float, default=0.0)
    parser.add_argument("--max-citation-regression-percent", type=float, default=0.0)
    parser.add_argument("--checkpoint", default="", help="Atomic progress checkpoint path")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume completed cases from --checkpoint",
    )
    parser.add_argument("--fail-on-regression", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.model:
        raise SystemExit("--model or CGA_EVAL_MODEL is required")
    api_key = os.getenv(args.api_key_env, "")
    if not api_key:
        raise SystemExit(f"{args.api_key_env} is required")
    if args.resume and not args.checkpoint:
        raise SystemExit("--resume requires --checkpoint")

    cases = load_frozen_cases(Path(args.input))
    config = ModelRunConfig(
        model=args.model,
        base_url=args.base_url,
        temperature=args.temperature,
        seed=args.seed,
        timeout_seconds=args.timeout_seconds,
        requests_per_minute=args.requests_per_minute,
        max_retries=args.max_retries,
        retry_backoff_seconds=args.retry_backoff_seconds,
        max_retry_delay_seconds=args.max_retry_delay_seconds,
    )
    checkpoint_path = Path(args.checkpoint) if args.checkpoint else None
    resume_state = None
    if args.resume:
        if checkpoint_path is None or not checkpoint_path.exists():
            raise SystemExit(f"checkpoint not found: {checkpoint_path}")
        resume_state = load_answer_checkpoint(checkpoint_path)
    checkpoint_callback = None
    if checkpoint_path is not None:
        def checkpoint_callback(state: dict[str, Any]) -> None:
            write_answer_checkpoint(checkpoint_path, state)

    with OpenAICompatibleAnswerRunner(api_key=api_key, config=config) as runner:
        report = run_answer_benchmark(
            cases=cases,
            runner=runner,
            model_run=config.as_dict(),
            max_task_regression_percent=args.max_task_regression_percent,
            max_citation_regression_percent=args.max_citation_regression_percent,
            max_cases=args.max_cases,
            max_expansion_depth=args.max_expansion_depth,
            max_expansion_chunks=args.max_expansion_chunks,
            max_cg_context_tokens=args.max_cg_context_tokens,
            resume_state=resume_state,
            checkpoint_callback=checkpoint_callback,
        )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.markdown:
        markdown_path = Path(args.markdown)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_markdown(report), encoding="utf-8")

    print(f"Cases: {report['caseSet']['caseCount']}")
    print(f"Case set SHA-256: {report['caseSet']['sha256']}")
    print(f"Regression gates passed: {report['regressionGates']['allPassed']}")
    print(f"Report saved to: {output_path}")
    if args.fail_on_regression and not report["regressionGates"]["allPassed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()