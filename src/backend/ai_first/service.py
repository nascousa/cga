"""AI-first readiness scoring and evidence-pack assembly.

The first implementation is intentionally observe-only. It reads existing
project metadata, graph counts, indexing jobs, and work briefing activity, then
turns them into explainable readiness signals for team planning.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.auth import pgshim as aiosqlite
from backend.workbriefing.models import WorkActivity
from backend.workbriefing.service import WorkBriefingService


class AiFirstProjectNotFoundError(ValueError):
    """Raised when a requested project cannot be found."""


class AiFirstEvidencePackNotFoundError(ValueError):
    """Raised when a persisted evidence pack cannot be found."""


class AiFirstPolicyProfileError(ValueError):
    """Raised when policy profile input is invalid."""


class AiFirstSignalError(ValueError):
    """Raised when AI-first signal input is invalid."""


class AiFirstSignalImportError(ValueError):
    """Raised when external signal import cannot proceed."""


REQUIRED_ADC_FILES = (
    ".adc/index.md",
    ".adc/prompt-rules.md",
    ".adc/planning/status.md",
    ".adc/knowledge/known-issues.md",
    ".adc/knowledge/glossary.md",
)

GRAPH_COUNT_QUERIES = {
    "repositories": "MATCH (r:Repository) RETURN count(r)",
    "files": "MATCH (f:File) RETURN count(f)",
    "symbols": "MATCH (s:Symbol) RETURN count(s)",
    "variables": "MATCH (v:Variable) RETURN count(v)",
    "call_edges": "MATCH ()-[c:CALLS]->() RETURN count(c)",
    "import_edges": "MATCH ()-[i:IMPORTS]->() RETURN count(i)",
    "flow_edges": "MATCH ()-[f:FLOWS_TO]->() RETURN count(f)",
    "defines_edges": "MATCH ()-[d:DEFINES]->() RETURN count(d)",
    "contains_edges": "MATCH ()-[c:CONTAINS]->() RETURN count(c)",
}

TEST_PATHS = (
    "tests",
    "src/tests",
    "test",
    "__tests__",
)

TEST_CONFIG_FILES = (
    "pytest.ini",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
)

SECRET_KEY_FRAGMENTS = (
    "authorization",
    "bearer",
    "cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
)

TRACE_STREAM = "contextgraph:traces"
TRACE_SCAN_LIMIT = 500
CORRELATION_KEYS = ("task_id", "issue_id", "pr_id", "activity_id")
CORRELATION_METADATA_ALIASES = {
    "task_id": ("task_id", "taskId", "task", "taskId"),
    "issue_id": ("issue_id", "issueId", "issue", "pbi", "pbi_id", "work_item", "workItemId"),
    "pr_id": ("pr_id", "prId", "pr", "pull_request", "pullRequest", "pull_request_id"),
    "activity_id": ("activity_id", "activityId", "work_activity_id", "workActivityId"),
}

POLICY_PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "observe-only": {
        "name": "observe-only",
        "label": "Observe Only",
        "enforcement_level": "L0",
        "decision": "record_only",
        "description": "Record readiness and evidence without warning or blocking workflows.",
        "tool_policy": {
            "default": "auto",
            "read_tools": "auto",
            "write_tools": "suggest",
            "sensitive_tools": "blocked",
        },
    },
    "local-dev": {
        "name": "local-dev",
        "label": "Local Dev",
        "enforcement_level": "L0",
        "decision": "developer_controlled",
        "description": "High-trust local development profile with evidence recording enabled.",
        "tool_policy": {
            "default": "auto",
            "read_tools": "auto",
            "write_tools": "suggest",
            "sensitive_tools": "blocked",
        },
    },
    "team-default": {
        "name": "team-default",
        "label": "Team Default",
        "enforcement_level": "L1",
        "decision": "warn_on_missing_evidence",
        "description": "Team workflow profile that warns when evidence or validation is missing.",
        "tool_policy": {
            "default": "suggest",
            "read_tools": "auto",
            "write_tools": "approval_required",
            "sensitive_tools": "blocked",
        },
    },
    "regulated": {
        "name": "regulated",
        "label": "Regulated",
        "enforcement_level": "L3",
        "decision": "approval_gates",
        "description": "Strict profile for regulated engineering workflows requiring approval gates.",
        "tool_policy": {
            "default": "approval_required",
            "read_tools": "suggest",
            "write_tools": "approval_required",
            "sensitive_tools": "blocked",
        },
    },
    "sovereign": {
        "name": "sovereign",
        "label": "Sovereign",
        "enforcement_level": "L4",
        "decision": "sovereign_boundary_required",
        "description": "Sovereign-cloud profile for strict data boundary, endpoint, and tool constraints.",
        "tool_policy": {
            "default": "approval_required",
            "read_tools": "suggest",
            "write_tools": "approval_required",
            "sensitive_tools": "blocked",
            "external_endpoints": "blocked_unless_approved",
        },
    },
    "offline-isolated": {
        "name": "offline-isolated",
        "label": "Offline Isolated",
        "enforcement_level": "L4",
        "decision": "local_only",
        "description": "Offline profile for isolated environments with no external network assumptions.",
        "tool_policy": {
            "default": "suggest",
            "read_tools": "auto_local_only",
            "write_tools": "approval_required",
            "sensitive_tools": "blocked",
            "external_endpoints": "blocked",
        },
    },
}

SIGNAL_TYPE_DEFINITIONS: dict[str, dict[str, str]] = {
    "ci": {
        "label": "CI",
        "dimension": "verification",
        "description": "Continuous integration or local validation result.",
    },
    "pr": {
        "label": "PR",
        "dimension": "workflow",
        "description": "Pull request, review, merge, or review-latency signal.",
    },
    "benchmark": {
        "label": "Benchmark",
        "dimension": "roi",
        "description": "Token/HPS/quality/cost benchmark signal.",
    },
}

OK_SIGNAL_STATUSES = {"ok", "pass", "passed", "success", "succeeded", "green", "merged", "approved", "ready"}
WARN_SIGNAL_STATUSES = {"warn", "warning", "pending", "running", "open", "review", "queued", "unknown"}
FAIL_SIGNAL_STATUSES = {"fail", "failed", "failure", "error", "red", "blocked", "rejected"}


async def build_readiness_response(
    *,
    db: aiosqlite.Connection,
    registry: Any,
    consumer: Any,
    work_briefing_service: WorkBriefingService,
    project_id: str | None = None,
) -> dict[str, Any]:
    projects = await _load_projects(db, project_id=project_id)
    snapshots = []
    for project in projects:
        snapshots.append(
            await build_project_readiness_snapshot(
                db=db,
                registry=registry,
                consumer=consumer,
                work_briefing_service=work_briefing_service,
                project=project,
            )
        )
    return {
        "generated_at": _utc_now(),
        "project_id": project_id or None,
        "count": len(snapshots),
        "projects": snapshots,
    }


async def build_project_readiness_snapshot(
    *,
    db: aiosqlite.Connection,
    registry: Any,
    consumer: Any,
    work_briefing_service: WorkBriefingService,
    project: dict[str, Any],
) -> dict[str, Any]:
    repo_path = str(project.get("repo_path") or "").strip()
    repo = _scan_repo(repo_path)
    adc = _scan_adc(repo_path) if repo["exists"] else _empty_adc_scan(repo_path)
    verification = _scan_verification(repo_path) if repo["exists"] else _empty_verification_scan(repo_path)
    graph_stats = _collect_graph_stats(registry, str(project["project_name"]))
    latest_index_job = await _latest_index_job(consumer, repo_path)
    activity_count = await _safe_activity_count(work_briefing_service, str(project["project_id"]))
    token_count = await _active_mcp_token_count(db, int(project["id"]))
    policy_profile = await _policy_profile_for_project(db, project)
    ci_signal = await _latest_signal_for_project(db, project, "ci")
    pr_signal = await _latest_signal_for_project(db, project, "pr")
    benchmark_signal = await _latest_signal_for_project(db, project, "benchmark")
    latest_evidence_pack = await _latest_evidence_pack_for_project(db, project)

    dimensions = [
        _dimension(
            "context",
            "Context Readiness",
            [
                _signal(
                    "repo_path",
                    "Repository path",
                    "ok" if repo["exists"] else "fail" if repo_path else "unknown",
                    "Repository path exists" if repo["exists"] else "Repository path is not resolvable",
                    evidence={"repo_path": repo_path, "exists": repo["exists"]},
                    recommendation=None if repo["exists"] else "Set a valid repository path on the CGA project.",
                ),
                _signal(
                    "adc_core_files",
                    "ADC core files",
                    _coverage_status(adc["present_count"], adc["required_count"]),
                    f"{adc['present_count']} of {adc['required_count']} required ADC files found",
                    evidence={"present": adc["present"], "missing": adc["missing"]},
                    recommendation=None if not adc["missing"] else "Add the missing ADC files so agents can load team rules and context.",
                ),
                _signal(
                    "graph_index",
                    "Graph index",
                    _graph_status(graph_stats),
                    _graph_summary(graph_stats),
                    evidence=graph_stats,
                    recommendation=None if _graph_status(graph_stats) == "ok" else "Run a full or incremental index for this project.",
                ),
                _signal(
                    "index_job",
                    "Latest index job",
                    _index_job_status(latest_index_job),
                    _index_job_summary(latest_index_job),
                    evidence=latest_index_job,
                    recommendation=None if _index_job_status(latest_index_job) == "ok" else "Check indexing status and recover or rerun stale/failed jobs.",
                ),
            ],
        ),
        _dimension(
            "verification",
            "Verification Readiness",
            [
                _signal(
                    "test_surface",
                    "Test surface",
                    "ok" if verification["has_tests"] else "warn" if repo["exists"] else "unknown",
                    "Test directories or known test config found" if verification["has_tests"] else "No obvious test surface found",
                    evidence=verification,
                    recommendation=None if verification["has_tests"] else "Document deterministic validation commands or add tests before agent-heavy work.",
                ),
                _signal(
                    "ci_workflows",
                    "CI workflows",
                    "ok" if verification["ci_workflow_count"] else "warn" if repo["exists"] else "unknown",
                    f"{verification['ci_workflow_count']} workflow files detected",
                    evidence={"workflow_files": verification["ci_workflows"]},
                    recommendation=None if verification["ci_workflow_count"] else "Add or document CI feedback so agents and reviewers get deterministic signals.",
                ),
                _signal(
                    "ci_signal",
                    "Latest CI signal",
                    _readiness_status_from_signal(ci_signal),
                    _signal_summary(ci_signal, "No CI or validation signal recorded yet"),
                    evidence=ci_signal or {},
                    recommendation=None if ci_signal else "Record a CI or validation signal after the next agent-assisted change.",
                ),
            ],
        ),
        _dimension(
            "workflow",
            "Workflow Readiness",
            [
                _signal(
                    "work_briefing",
                    "Work briefing activity",
                    "ok" if activity_count > 0 else "warn",
                    f"{activity_count} recent work activity records available",
                    evidence={"activity_count": activity_count},
                    recommendation=None if activity_count > 0 else "Start publishing agent and human workflow activity into CGA Work Briefing.",
                ),
                _signal(
                    "learn_prepare_execute",
                    "Learn / Prepare / Execute evidence",
                    "unknown",
                    "Cycle-level AI-first learning evidence is not yet modeled",
                    recommendation="Capture each AI-first cycle as work briefing activity with tags such as ai-first, learn, prepare, or execute.",
                ),
                _signal(
                    "pr_signal",
                    "Latest PR/review signal",
                    _readiness_status_from_signal(pr_signal),
                    _signal_summary(pr_signal, "No PR or review signal recorded yet"),
                    evidence=pr_signal or {},
                    recommendation=None if pr_signal else "Record PR/review status or review-latency evidence for AI-assisted work.",
                ),
            ],
        ),
        _dimension(
            "governance",
            "Governance Readiness",
            [
                _signal(
                    "mcp_token",
                    "Project MCP token",
                    "ok" if token_count > 0 else "warn",
                    f"{token_count} active MCP token(s) configured",
                    evidence={"active_mcp_tokens": token_count},
                    recommendation=None if token_count > 0 else "Create a project-scoped MCP token or configure account relay access.",
                ),
                _signal(
                    "policy_profile",
                    "Policy profile",
                    "warn" if policy_profile["name"] == "observe-only" else "ok",
                    f"{policy_profile['label']} profile ({policy_profile['enforcement_level']})",
                    evidence=policy_profile,
                    recommendation=None if policy_profile["name"] != "observe-only" else "Select a project-level policy profile before regulated or high-risk AI-first workflows.",
                ),
            ],
        ),
        _dimension(
            "roi",
            "ROI & Outcome Readiness",
            [
                _signal(
                    "context_efficiency",
                    "Context efficiency baseline",
                    _readiness_status_from_signal(benchmark_signal) if benchmark_signal else "warn" if _graph_status(graph_stats) == "ok" else "unknown",
                    _signal_summary(benchmark_signal, "Graph context is available, but token/HPS benchmark evidence is not connected yet" if _graph_status(graph_stats) == "ok" else "Graph-backed context benchmark is not available yet"),
                    evidence=benchmark_signal or {"graph_available": _graph_status(graph_stats) == "ok"},
                    recommendation=None if benchmark_signal else "Run or record the context-quality benchmark to establish token reduction and HPS deltas for this project.",
                ),
                _signal(
                    "outcome_metrics",
                    "Outcome metrics",
                    "warn" if pr_signal or ci_signal else "unknown",
                    "PR/CI signals are connected; richer rework, defect escape, and cost metrics are still pending" if pr_signal or ci_signal else "PR latency, rework, defect escape, and cost per successful change are not connected yet",
                    evidence={"has_pr_signal": bool(pr_signal), "has_ci_signal": bool(ci_signal)},
                    recommendation="Connect richer PR/CI metadata after the signal ingestion MVP proves useful for one lighthouse repo.",
                ),
            ],
        ),
    ]

    overall_score = _average_known_scores([dimension["score"] for dimension in dimensions])
    policy_gates = _evaluate_policy_gates(
        policy_profile=policy_profile,
        latest_evidence_pack=latest_evidence_pack,
        ci_signal=ci_signal,
        pr_signal=pr_signal,
        benchmark_signal=benchmark_signal,
        activity_count=activity_count,
    )
    return {
        "project": {
            "id": int(project["id"]),
            "project_name": project["project_name"],
            "project_id": project["project_id"],
            "repo_path": repo_path,
            "is_active": bool(project["is_active"]),
        },
        "generated_at": _utc_now(),
        "overall_score": overall_score,
        "overall_status": _status_from_score(overall_score),
        "dimensions": dimensions,
        "policy_profile": policy_profile,
        "policy_gates": policy_gates,
        "signals": {
            "ci": ci_signal,
            "pr": pr_signal,
            "benchmark": benchmark_signal,
        },
        "recommended_next_actions": _recommended_next_actions(dimensions),
    }


async def build_evidence_pack(
    *,
    db: aiosqlite.Connection,
    registry: Any,
    consumer: Any,
    work_briefing_service: WorkBriefingService,
    project_id: str,
    limit: int = 25,
    task_id: str | None = None,
    issue_id: str | None = None,
    pr_id: str | None = None,
    activity_id: str | None = None,
    trace_redis_url: str | None = None,
) -> dict[str, Any]:
    projects = await _load_projects(db, project_id=project_id)
    if not projects:
        raise AiFirstProjectNotFoundError(f"Project not found: {project_id}")
    project = projects[0]
    readiness = await build_project_readiness_snapshot(
        db=db,
        registry=registry,
        consumer=consumer,
        work_briefing_service=work_briefing_service,
        project=project,
    )
    correlation_filters = _normalize_correlation_filters(
        task_id=task_id,
        issue_id=issue_id,
        pr_id=pr_id,
        activity_id=activity_id,
    )
    activities = await _safe_recent_activities(
        work_briefing_service,
        str(project["project_id"]),
        _activity_fetch_limit(limit, correlation_filters),
    )
    matched_activities = _filter_activities_by_correlation(activities, correlation_filters)[:limit]
    sanitized_activities = [_sanitize_activity(activity) for activity in activities]
    sanitized_matched_activities = [_sanitize_activity(activity) for activity in matched_activities]
    signals = await _recent_signals_for_project(db, project, _activity_fetch_limit(limit, correlation_filters))
    matched_signals = _filter_signals_by_correlation(signals, correlation_filters)[:limit]
    trace_evidence = _safe_trace_evidence(
        trace_redis_url=trace_redis_url,
        correlation_filters=correlation_filters,
        limit=limit,
    )
    policy_profile = readiness.get("policy_profile") or _default_policy_profile()
    pack = {
        "schema_version": "ai-first-evidence-pack.v0",
        "generated_at": _utc_now(),
        "correlation": {
            "mode": "task_bound" if correlation_filters else "project_recent",
            "filters": correlation_filters,
            "activity_match_count": len(sanitized_matched_activities),
            "trace_match_count": trace_evidence.get("count", 0),
        },
        "policy_profile": policy_profile,
        "project": readiness["project"],
        "readiness": {
            "overall_score": readiness["overall_score"],
            "overall_status": readiness["overall_status"],
            "dimensions": [
                {
                    "key": dimension["key"],
                    "label": dimension["label"],
                    "score": dimension["score"],
                    "status": dimension["status"],
                }
                for dimension in readiness["dimensions"]
            ],
            "recommended_next_actions": readiness["recommended_next_actions"],
        },
        "policy_gates": readiness.get("policy_gates") or {},
        "activity_evidence": {
            "count": len(sanitized_matched_activities),
            "total_recent_scanned": len(sanitized_activities),
            "limit": limit,
            "activities": sanitized_matched_activities,
        },
        "signal_evidence": {
            "count": len(matched_signals),
            "total_recent_scanned": len(signals),
            "limit": limit,
            "signals": [_sanitize_signal_for_evidence(signal) for signal in matched_signals],
        },
        "trace_evidence": {
            **trace_evidence,
            "stream": TRACE_STREAM,
        },
        "redaction": {
            "mode": "summary_only",
            "raw_metadata": "metadata keys only; sensitive-looking values are not exported",
            "trace_args": "argument keys and explicit correlation fields only; raw arguments are not exported",
        },
    }
    pack["markdown"] = _render_evidence_markdown(pack)
    return pack


async def list_policy_profiles(
    *,
    db: aiosqlite.Connection,
    project_id: str | None = None,
) -> dict[str, Any]:
    projects = await _load_projects(db, project_id=project_id)
    profiles = [await _policy_profile_for_project(db, project) for project in projects]
    return {
        "project_id": project_id or None,
        "definitions": list(POLICY_PROFILE_DEFINITIONS.values()),
        "count": len(profiles),
        "profiles": profiles,
    }


async def update_policy_profile(
    *,
    db: aiosqlite.Connection,
    project_id: str,
    profile_name: str,
    updated_by: str = "",
    notes: str = "",
) -> dict[str, Any]:
    cleaned_profile = str(profile_name or "").strip()
    if cleaned_profile not in POLICY_PROFILE_DEFINITIONS:
        raise AiFirstPolicyProfileError(f"Unknown AI-first policy profile: {profile_name}")
    projects = await _load_projects(db, project_id=project_id)
    if not projects:
        raise AiFirstProjectNotFoundError(f"Project not found: {project_id}")
    project = projects[0]
    definition = POLICY_PROFILE_DEFINITIONS[cleaned_profile]
    now = _utc_now()
    tool_policy_json = json.dumps(definition["tool_policy"], sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    async with db.execute(
        """
        INSERT INTO ai_first_policy_profiles(
            project_id,
            profile_name,
            enforcement_level,
            tool_policy_json,
            notes,
            updated_by,
            created_at,
            updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(project_id) DO UPDATE SET
            profile_name = EXCLUDED.profile_name,
            enforcement_level = EXCLUDED.enforcement_level,
            tool_policy_json = EXCLUDED.tool_policy_json,
            notes = EXCLUDED.notes,
            updated_by = EXCLUDED.updated_by,
            updated_at = EXCLUDED.updated_at
        RETURNING id, project_id, profile_name, enforcement_level, tool_policy_json, notes, updated_by, created_at, updated_at
        """,
        (
            int(project["id"]),
            cleaned_profile,
            definition["enforcement_level"],
            tool_policy_json,
            str(notes or ""),
            str(updated_by or ""),
            now,
            now,
        ),
    ) as cur:
        row = await cur.fetchone()
    await db.commit()
    return _policy_profile_from_row(project, dict(row))


async def record_signal(
    *,
    db: aiosqlite.Connection,
    project_id: str,
    signal_type: str,
    name: str = "",
    status: str = "unknown",
    value: Any = "",
    unit: str = "",
    source_url: str = "",
    metadata: dict[str, Any] | None = None,
    observed_at: str | None = None,
    created_by: str = "",
) -> dict[str, Any]:
    cleaned_type = str(signal_type or "").strip().lower()
    if cleaned_type not in SIGNAL_TYPE_DEFINITIONS:
        raise AiFirstSignalError(f"Unknown AI-first signal type: {signal_type}")
    projects = await _load_projects(db, project_id=project_id)
    if not projects:
        raise AiFirstProjectNotFoundError(f"Project not found: {project_id}")
    project = projects[0]
    signal_id = f"sig-{uuid.uuid4().hex}"
    now = _utc_now()
    observed = _normalize_iso_datetime(observed_at) or now
    metadata_json = json.dumps(metadata or {}, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)
    async with db.execute(
        """
        INSERT INTO ai_first_signals(
            signal_id,
            project_id,
            project_external_id,
            project_name,
            signal_type,
            name,
            status,
            value_text,
            unit,
            source_url,
            metadata_json,
            observed_at,
            created_by,
            created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        RETURNING id, signal_id, project_id, project_external_id, project_name,
                  signal_type, name, status, value_text, unit, source_url,
                  metadata_json, observed_at, created_by, created_at
        """,
        (
            signal_id,
            int(project["id"]),
            str(project.get("project_id") or ""),
            str(project.get("project_name") or ""),
            cleaned_type,
            str(name or ""),
            str(status or "unknown").strip().lower() or "unknown",
            str(value if value is not None else ""),
            str(unit or ""),
            str(source_url or ""),
            metadata_json,
            observed,
            str(created_by or ""),
            now,
        ),
    ) as cur:
        row = await cur.fetchone()
    await db.commit()
    return _signal_from_row(dict(row))


async def list_signals(
    *,
    db: aiosqlite.Connection,
    project_id: str | None = None,
    signal_type: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit or 25), 200))
    where: list[str] = []
    params: list[Any] = []
    if project_id:
        cleaned_project = project_id.strip()
        where.append("(project_external_id = ? OR project_name = ?)")
        params.extend([cleaned_project, cleaned_project])
    if signal_type:
        cleaned_type = signal_type.strip().lower()
        where.append("signal_type = ?")
        params.append(cleaned_type)
    params.append(safe_limit)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    async with db.execute(
        f"""
        SELECT id, signal_id, project_id, project_external_id, project_name,
               signal_type, name, status, value_text, unit, source_url,
               metadata_json, observed_at, created_by, created_at
        FROM ai_first_signals
        {where_sql}
        ORDER BY observed_at DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    ) as cur:
        rows = await cur.fetchall()
    return {
        "project_id": project_id or None,
        "signal_type": signal_type or None,
        "definitions": SIGNAL_TYPE_DEFINITIONS,
        "count": len(rows),
        "signals": [_signal_from_row(dict(row)) for row in rows],
    }


async def import_github_signals(
    *,
    db: aiosqlite.Connection,
    project_id: str,
    repo_url: str | None = None,
    limit: int = 5,
    created_by: str = "",
    github_token: str | None = None,
    http_get_json: Any = None,
) -> dict[str, Any]:
    projects = await _load_projects(db, project_id=project_id)
    if not projects:
        raise AiFirstProjectNotFoundError(f"Project not found: {project_id}")
    project = projects[0]
    slug = _resolve_github_slug(project, repo_url)
    if not slug:
        raise AiFirstSignalImportError("GitHub repository URL could not be resolved for this project")
    owner, repo = slug
    safe_limit = max(1, min(int(limit or 5), 20))
    token = github_token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "cga-ai-first-signals",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    runs_url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs?per_page={safe_limit}"
    prs_url = f"https://api.github.com/repos/{owner}/{repo}/pulls?state=all&sort=updated&direction=desc&per_page={safe_limit}"
    runs_payload = await _github_get_json(runs_url, headers=headers, http_get_json=http_get_json)
    prs_payload = await _github_get_json(prs_url, headers=headers, http_get_json=http_get_json)

    imported: list[dict[str, Any]] = []
    for run in (runs_payload.get("workflow_runs") or [])[:safe_limit]:
        signal_status = _github_run_status(run)
        imported.append(
            await record_signal(
                db=db,
                project_id=str(project["project_id"]),
                signal_type="ci",
                name=f"GitHub Actions: {run.get('name') or run.get('display_title') or 'workflow'}",
                status=signal_status,
                value=run.get("run_number") or run.get("id") or "",
                unit="run",
                source_url=str(run.get("html_url") or ""),
                observed_at=str(run.get("updated_at") or run.get("run_started_at") or run.get("created_at") or ""),
                created_by=created_by,
                metadata={
                    "provider": "github",
                    "github_id": run.get("id"),
                    "workflow_id": run.get("workflow_id"),
                    "head_branch": run.get("head_branch"),
                    "head_sha": run.get("head_sha"),
                    "event": run.get("event"),
                },
            )
        )
    for pr in (prs_payload if isinstance(prs_payload, list) else [])[:safe_limit]:
        number = pr.get("number")
        imported.append(
            await record_signal(
                db=db,
                project_id=str(project["project_id"]),
                signal_type="pr",
                name=f"GitHub PR #{number}: {pr.get('title') or ''}".strip(),
                status=_github_pr_status(pr),
                value=number or "",
                unit="pr",
                source_url=str(pr.get("html_url") or ""),
                observed_at=str(pr.get("updated_at") or pr.get("created_at") or ""),
                created_by=created_by,
                metadata={
                    "provider": "github",
                    "pr_id": str(number or ""),
                    "issue_id": str(number or ""),
                    "merged_at": pr.get("merged_at"),
                    "state": pr.get("state"),
                    "author": (pr.get("user") or {}).get("login") if isinstance(pr.get("user"), dict) else None,
                },
            )
        )
    return {
        "project_id": project["project_id"],
        "repository": f"{owner}/{repo}",
        "imported_count": len(imported),
        "signals": imported,
    }


async def save_evidence_pack(
    *,
    db: aiosqlite.Connection,
    pack: dict[str, Any],
    created_by: str = "",
) -> dict[str, Any]:
    evidence_id = f"ev-{uuid.uuid4().hex}"
    created_at = _utc_now()
    project = pack.get("project") or {}
    correlation = pack.get("correlation") or {}
    stored_pack = {
        **pack,
        "evidence_id": evidence_id,
        "persisted_at": created_at,
        "status": "generated",
    }
    evidence_json = json.dumps(stored_pack, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    correlation_json = json.dumps(correlation, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    markdown = str(pack.get("markdown") or "")
    async with db.execute(
        """
        INSERT INTO ai_first_evidence_packs(
            evidence_id,
            project_id,
            project_external_id,
            project_name,
            status,
            correlation_json,
            evidence_json,
            markdown,
            created_by,
            created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        RETURNING id, evidence_id, project_id, project_external_id, project_name, status, created_by, created_at
        """,
        (
            evidence_id,
            project.get("id"),
            str(project.get("project_id") or ""),
            str(project.get("project_name") or ""),
            "generated",
            correlation_json,
            evidence_json,
            markdown,
            created_by,
            created_at,
        ),
    ) as cur:
        row = await cur.fetchone()
    await db.commit()
    return _evidence_summary_from_row(dict(row), correlation=correlation, pack=stored_pack)


async def list_evidence_packs(
    *,
    db: aiosqlite.Connection,
    project_id: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit or 25), 200))
    params: tuple[Any, ...]
    where_sql = ""
    if project_id:
        cleaned = project_id.strip()
        where_sql = "WHERE project_external_id = ? OR project_name = ?"
        params = (cleaned, cleaned, safe_limit)
        limit_placeholder = "?"
    else:
        params = (safe_limit,)
        limit_placeholder = "?"
    async with db.execute(
        f"""
        SELECT id, evidence_id, project_id, project_external_id, project_name, status,
               correlation_json, created_by, created_at
        FROM ai_first_evidence_packs
        {where_sql}
        ORDER BY created_at DESC, id DESC
        LIMIT {limit_placeholder}
        """,
        params,
    ) as cur:
        rows = await cur.fetchall()
    return {
        "project_id": project_id or None,
        "count": len(rows),
        "evidence_packs": [_evidence_summary_from_row(dict(row)) for row in rows],
    }


async def get_evidence_pack(
    *,
    db: aiosqlite.Connection,
    evidence_id: str,
) -> dict[str, Any]:
    async with db.execute(
        """
        SELECT id, evidence_id, project_id, project_external_id, project_name, status,
               correlation_json, evidence_json, markdown, created_by, created_at
        FROM ai_first_evidence_packs
        WHERE evidence_id = ?
        """,
        (evidence_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise AiFirstEvidencePackNotFoundError(f"Evidence pack not found: {evidence_id}")
    data = dict(row)
    try:
        pack = json.loads(data.get("evidence_json") or "{}")
    except Exception:
        pack = {}
    return {
        **_evidence_summary_from_row(data, pack=pack),
        "evidence": pack,
        "markdown": data.get("markdown") or pack.get("markdown") or "",
    }


async def _latest_evidence_pack_for_project(db: aiosqlite.Connection, project: dict[str, Any]) -> dict[str, Any] | None:
    async with db.execute(
        """
        SELECT id, evidence_id, project_id, project_external_id, project_name, status,
               correlation_json, created_by, created_at
        FROM ai_first_evidence_packs
        WHERE project_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (int(project["id"]),),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return _evidence_summary_from_row(dict(row))


def _evaluate_policy_gates(
    *,
    policy_profile: dict[str, Any],
    latest_evidence_pack: dict[str, Any] | None,
    ci_signal: dict[str, Any] | None,
    pr_signal: dict[str, Any] | None,
    benchmark_signal: dict[str, Any] | None,
    activity_count: int,
) -> dict[str, Any]:
    profile_name = policy_profile.get("name") or "observe-only"
    enforcement_level = policy_profile.get("enforcement_level") or "L0"
    if enforcement_level == "L0":
        return {
            "profile_name": profile_name,
            "enforcement_level": enforcement_level,
            "overall_status": "ok",
            "mode": "observe",
            "gates": [],
        }

    severity = "required" if enforcement_level in {"L3", "L4"} else "warning"
    gates = [
        _policy_gate(
            key="saved_evidence_pack",
            label="Saved evidence pack",
            status="ok" if latest_evidence_pack else "warn",
            severity=severity,
            summary=(
                f"Latest evidence pack {latest_evidence_pack['evidence_id']} saved at {latest_evidence_pack['created_at']}"
                if latest_evidence_pack
                else "No saved evidence pack is available for this project yet"
            ),
            evidence=latest_evidence_pack or {},
            recommendation=None if latest_evidence_pack else "Save an evidence pack for review or release/audit use.",
        ),
        _policy_gate_for_signal("ci_signal", "CI signal", ci_signal, severity, "Record or import CI status before review."),
        _policy_gate_for_signal("pr_signal", "PR/review signal", pr_signal, severity, "Record or import PR/review status for the task."),
        _policy_gate_for_signal("benchmark_signal", "Benchmark signal", benchmark_signal, "warning", "Record a token/HPS benchmark signal for ROI evidence."),
        _policy_gate(
            key="work_briefing_activity",
            label="Work Briefing activity",
            status="ok" if activity_count > 0 else "warn",
            severity="warning",
            summary=f"{activity_count} recent work activity records available" if activity_count > 0 else "No work activity has been recorded for this project",
            evidence={"activity_count": activity_count},
            recommendation=None if activity_count > 0 else "Publish task progress or validation activity into Work Briefing.",
        ),
    ]
    overall_status = "fail" if any(gate["status"] == "fail" for gate in gates) else "warn" if any(gate["status"] == "warn" for gate in gates) else "ok"
    return {
        "profile_name": profile_name,
        "enforcement_level": enforcement_level,
        "overall_status": overall_status,
        "mode": "warning_gates",
        "gates": gates,
    }


def _policy_gate_for_signal(
    key: str,
    label: str,
    signal: dict[str, Any] | None,
    severity: str,
    missing_recommendation: str,
) -> dict[str, Any]:
    if not signal:
        return _policy_gate(
            key=key,
            label=label,
            status="warn",
            severity=severity,
            summary=f"No {label.lower()} is available yet",
            evidence={},
            recommendation=missing_recommendation,
        )
    return _policy_gate(
        key=key,
        label=label,
        status=_readiness_status_from_signal(signal),
        severity=severity,
        summary=_signal_summary(signal, f"No {label.lower()} is available yet"),
        evidence=signal,
        recommendation=None if _readiness_status_from_signal(signal) == "ok" else missing_recommendation,
    )


def _policy_gate(
    *,
    key: str,
    label: str,
    status: str,
    severity: str,
    summary: str,
    evidence: dict[str, Any],
    recommendation: str | None = None,
) -> dict[str, Any]:
    gate = {
        "key": key,
        "label": label,
        "status": status,
        "severity": severity,
        "summary": summary,
        "evidence": evidence,
    }
    if recommendation:
        gate["recommendation"] = recommendation
    return gate


def _normalize_correlation_filters(
    *,
    task_id: str | None = None,
    issue_id: str | None = None,
    pr_id: str | None = None,
    activity_id: str | None = None,
) -> dict[str, str]:
    raw = {
        "task_id": task_id,
        "issue_id": issue_id,
        "pr_id": pr_id,
        "activity_id": activity_id,
    }
    return {
        key: str(value).strip()
        for key, value in raw.items()
        if isinstance(value, str) and str(value).strip()
    }


def _activity_fetch_limit(limit: int, correlation_filters: dict[str, str]) -> int:
    safe_limit = max(1, min(int(limit or 25), 5000))
    if not correlation_filters:
        return safe_limit
    return max(safe_limit, min(5000, safe_limit * 10, 250))


def _filter_activities_by_correlation(
    activities: list[WorkActivity],
    correlation_filters: dict[str, str],
) -> list[WorkActivity]:
    if not correlation_filters:
        return activities
    return [activity for activity in activities if _activity_matches_correlation(activity, correlation_filters)]


def _activity_matches_correlation(activity: WorkActivity, correlation_filters: dict[str, str]) -> bool:
    for key, value in correlation_filters.items():
        if not _matches_filter_value(_activity_correlation_values(activity, key), value):
            return False
    return True


async def _recent_signals_for_project(
    db: aiosqlite.Connection,
    project: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 25), 5000))
    async with db.execute(
        """
        SELECT id, signal_id, project_id, project_external_id, project_name,
               signal_type, name, status, value_text, unit, source_url,
               metadata_json, observed_at, created_by, created_at
        FROM ai_first_signals
        WHERE project_id = ?
        ORDER BY observed_at DESC, id DESC
        LIMIT ?
        """,
        (int(project["id"]), safe_limit),
    ) as cur:
        rows = await cur.fetchall()
    return [_signal_from_row(dict(row), include_metadata=True) for row in rows]


def _filter_signals_by_correlation(
    signals: list[dict[str, Any]],
    correlation_filters: dict[str, str],
) -> list[dict[str, Any]]:
    if not correlation_filters:
        return signals
    return [signal for signal in signals if _signal_matches_correlation(signal, correlation_filters)]


def _signal_matches_correlation(signal: dict[str, Any], correlation_filters: dict[str, str]) -> bool:
    for key, value in correlation_filters.items():
        if not _matches_filter_value(_signal_correlation_values(signal, key), value):
            return False
    return True


def _signal_correlation_values(signal: dict[str, Any], key: str) -> list[str]:
    values = [
        signal.get("signal_id") or "",
        signal.get("signal_type") or "",
        signal.get("name") or "",
        signal.get("status") or "",
        signal.get("value") or "",
        signal.get("source_url") or "",
    ]
    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    for alias in CORRELATION_METADATA_ALIASES.get(key, (key,)):
        if alias in metadata:
            values.extend(_flatten_scalar_values(metadata.get(alias)))
    values.extend(_flatten_scalar_values(metadata.get("correlation")))
    return values


def _activity_correlation_values(activity: WorkActivity, key: str) -> list[str]:
    values: list[str] = [
        activity.activity_id,
        activity.source_item_id,
        activity.event_type,
        activity.title,
        activity.summary,
        activity.body_text,
        activity.source_url or "",
        *activity.tags,
    ]
    metadata = activity.raw_metadata if isinstance(activity.raw_metadata, dict) else {}
    for alias in CORRELATION_METADATA_ALIASES.get(key, (key,)):
        if alias in metadata:
            values.extend(_flatten_scalar_values(metadata.get(alias)))
    values.extend(_flatten_scalar_values(metadata.get("correlation")))
    return values


def _safe_trace_evidence(
    *,
    trace_redis_url: str | None,
    correlation_filters: dict[str, str],
    limit: int,
) -> dict[str, Any]:
    if not correlation_filters:
        return {"status": "not_requested", "count": 0, "traces": []}
    if not trace_redis_url:
        return {"status": "not_configured", "count": 0, "traces": []}
    try:
        import redis

        client = redis.from_url(trace_redis_url, decode_responses=True, socket_connect_timeout=2)
        try:
            entries = client.xrevrange(TRACE_STREAM, count=TRACE_SCAN_LIMIT)
        finally:
            client.close()
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc), "count": 0, "traces": []}

    traces: list[dict[str, Any]] = []
    for _message_id, data in entries:
        try:
            payload = json.loads(data.get("payload", "{}"))
        except Exception:
            continue
        if not _trace_matches_correlation(payload, correlation_filters):
            continue
        traces.append(_sanitize_trace(payload))
        if len(traces) >= limit:
            break
    return {"status": "ok", "count": len(traces), "scanned": len(entries), "traces": traces}


def _trace_matches_correlation(payload: dict[str, Any], correlation_filters: dict[str, str]) -> bool:
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    for key, value in correlation_filters.items():
        values = _trace_correlation_values(args, key)
        if key == "activity_id":
            values.append(str(payload.get("trace_id") or ""))
        if not _matches_filter_value(values, value):
            return False
    return True


def _trace_correlation_values(args: dict[str, Any], key: str) -> list[str]:
    values: list[str] = []
    for alias in CORRELATION_METADATA_ALIASES.get(key, (key,)):
        if alias in args:
            values.extend(_flatten_scalar_values(args.get(alias)))
    values.extend(_flatten_scalar_values(args.get("correlation")))
    return values


def _sanitize_trace(payload: dict[str, Any]) -> dict[str, Any]:
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    return {
        "trace_id": str(payload.get("trace_id") or ""),
        "tool": str(payload.get("tool") or ""),
        "timestamp": _timestamp_to_iso(payload.get("ts")),
        "latency_ms": payload.get("latency_ms"),
        "arg_keys": sorted(str(key) for key in args.keys() if not _is_sensitive_key(str(key))),
        "correlation": {
            key: _first_present_correlation_value(args, key)
            for key in CORRELATION_KEYS
            if _first_present_correlation_value(args, key)
        },
        "result_count": len(results),
        "result_preview": _trace_result_preview(results),
    }


def _first_present_correlation_value(args: dict[str, Any], key: str) -> str | None:
    for alias in CORRELATION_METADATA_ALIASES.get(key, (key,)):
        values = _flatten_scalar_values(args.get(alias))
        if values:
            return values[0]
    return None


def _trace_result_preview(results: list[Any]) -> list[str]:
    preview: list[str] = []
    for item in results[:5]:
        if isinstance(item, dict):
            value = item.get("qualified_name") or item.get("file_path") or item.get("summary")
        else:
            value = item
        if value is not None:
            preview.append(str(value)[:160])
    return preview


def _flatten_scalar_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float, bool)):
        return [str(value)]
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            items.extend(_flatten_scalar_values(item))
        return items
    if isinstance(value, dict):
        items: list[str] = []
        for item in value.values():
            items.extend(_flatten_scalar_values(item))
        return items
    return [str(value)]


def _matches_filter_value(values: list[str], expected: str) -> bool:
    needle = expected.strip().casefold()
    if not needle:
        return True
    return any(needle in value.casefold() for value in values if value)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.casefold()
    return any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS)


def _timestamp_to_iso(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


async def _load_projects(db: aiosqlite.Connection, project_id: str | None = None) -> list[dict[str, Any]]:
    params: tuple[Any, ...] = ()
    where_sql = "WHERE is_active = 1"
    if project_id:
        cleaned = project_id.strip()
        where_sql += " AND (project_id = ? OR project_name = ?)"
        params = (cleaned, cleaned)
    async with db.execute(
        f"""
        SELECT id, project_name, project_id, upstream_url, description, repo_path, created_at, is_active
        FROM projects
        {where_sql}
        ORDER BY id
        """,
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


def _default_policy_profile() -> dict[str, Any]:
    definition = POLICY_PROFILE_DEFINITIONS["observe-only"]
    return {
        **definition,
        "profile_name": definition["name"],
        "is_default": True,
        "notes": "",
        "updated_by": "",
        "updated_at": None,
    }


async def _policy_profile_for_project(db: aiosqlite.Connection, project: dict[str, Any]) -> dict[str, Any]:
    async with db.execute(
        """
        SELECT id, project_id, profile_name, enforcement_level, tool_policy_json,
               notes, updated_by, created_at, updated_at
        FROM ai_first_policy_profiles
        WHERE project_id = ?
        """,
        (int(project["id"]),),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        profile = _default_policy_profile()
        return {
            **profile,
            "project": _project_summary(project),
        }
    return _policy_profile_from_row(project, dict(row))


async def _latest_signal_for_project(
    db: aiosqlite.Connection,
    project: dict[str, Any],
    signal_type: str,
) -> dict[str, Any] | None:
    async with db.execute(
        """
        SELECT id, signal_id, project_id, project_external_id, project_name,
               signal_type, name, status, value_text, unit, source_url,
               metadata_json, observed_at, created_by, created_at
        FROM ai_first_signals
        WHERE project_id = ? AND signal_type = ?
        ORDER BY observed_at DESC, id DESC
        LIMIT 1
        """,
        (int(project["id"]), signal_type),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return _signal_from_row(dict(row))


def _signal_from_row(row: dict[str, Any], *, include_metadata: bool = False) -> dict[str, Any]:
    try:
        metadata = json.loads(row.get("metadata_json") or "{}")
    except Exception:
        metadata = {}
    signal = {
        "id": int(row["id"]),
        "signal_id": row.get("signal_id") or "",
        "project_db_id": row.get("project_id"),
        "project_id": row.get("project_external_id") or "",
        "project_name": row.get("project_name") or "",
        "signal_type": row.get("signal_type") or "",
        "label": SIGNAL_TYPE_DEFINITIONS.get(row.get("signal_type") or "", {}).get("label", row.get("signal_type") or ""),
        "name": row.get("name") or "",
        "status": row.get("status") or "unknown",
        "value": row.get("value_text") or "",
        "unit": row.get("unit") or "",
        "source_url": row.get("source_url") or "",
        "metadata_keys": sorted(str(key) for key in metadata.keys()) if isinstance(metadata, dict) else [],
        "observed_at": row.get("observed_at") or "",
        "created_by": row.get("created_by") or "",
        "created_at": row.get("created_at") or "",
    }
    if include_metadata:
        signal["metadata"] = metadata if isinstance(metadata, dict) else {}
    return signal


def _sanitize_signal_for_evidence(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in signal.items()
        if key != "metadata"
    }


def _resolve_github_slug(project: dict[str, Any], repo_url: str | None) -> tuple[str, str] | None:
    for candidate in [repo_url, project.get("upstream_url"), _git_remote_url(project.get("repo_path"))]:
        slug = _parse_github_slug(candidate)
        if slug:
            return slug
    return None


def _git_remote_url(repo_path: Any) -> str | None:
    if not repo_path:
        return None
    config_path = Path(str(repo_path)) / ".git" / "config"
    if not config_path.exists():
        return None
    try:
        content = config_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    match = re.search(r"url\s*=\s*(\S+)", content)
    return match.group(1) if match else None


def _parse_github_slug(value: Any) -> tuple[str, str] | None:
    text = str(value or "").strip()
    if not text:
        return None
    patterns = [
        r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?/?$",
        r"https?://www\.github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?/?$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group("owner"), match.group("repo")
    return None


async def _github_get_json(url: str, *, headers: dict[str, str], http_get_json: Any = None) -> Any:
    if http_get_json:
        return await http_get_json(url, headers)
    try:
        import httpx
    except Exception as exc:  # pragma: no cover - dependency should exist in runtime
        raise AiFirstSignalImportError("httpx is unavailable for GitHub import") from exc
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
    if response.status_code >= 400:
        raise AiFirstSignalImportError(f"GitHub import failed with HTTP {response.status_code}")
    return response.json()


def _github_run_status(run: dict[str, Any]) -> str:
    conclusion = str(run.get("conclusion") or "").lower()
    status = str(run.get("status") or "").lower()
    if conclusion in {"success", "neutral", "skipped"}:
        return "ok"
    if conclusion in {"failure", "timed_out", "cancelled", "action_required"}:
        return "fail"
    if status in {"queued", "in_progress", "waiting", "requested"}:
        return "pending"
    return conclusion or status or "unknown"


def _github_pr_status(pr: dict[str, Any]) -> str:
    if pr.get("merged_at"):
        return "merged"
    state = str(pr.get("state") or "unknown").lower()
    if state == "closed":
        return "ok"
    if state == "open":
        return "open"
    return state or "unknown"


def _readiness_status_from_signal(signal: dict[str, Any] | None) -> str:
    if not signal:
        return "unknown"
    status = str(signal.get("status") or "unknown").strip().lower()
    if status in OK_SIGNAL_STATUSES:
        return "ok"
    if status in FAIL_SIGNAL_STATUSES:
        return "fail"
    if status in WARN_SIGNAL_STATUSES:
        return "warn"
    return "warn"


def _signal_summary(signal: dict[str, Any] | None, fallback: str) -> str:
    if not signal:
        return fallback
    name = signal.get("name") or signal.get("label") or signal.get("signal_type") or "signal"
    value = signal.get("value") or ""
    unit = signal.get("unit") or ""
    value_suffix = f" - {value}{(' ' + unit) if unit else ''}" if value else ""
    return f"{name}: {signal.get('status', 'unknown')} at {signal.get('observed_at', 'unknown time')}{value_suffix}"


def _normalize_iso_datetime(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _policy_profile_from_row(project: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    profile_name = str(row.get("profile_name") or "observe-only")
    definition = POLICY_PROFILE_DEFINITIONS.get(profile_name, POLICY_PROFILE_DEFINITIONS["observe-only"])
    try:
        tool_policy = json.loads(row.get("tool_policy_json") or "{}")
    except Exception:
        tool_policy = definition["tool_policy"]
    return {
        **definition,
        "name": definition["name"],
        "profile_name": definition["name"],
        "enforcement_level": row.get("enforcement_level") or definition["enforcement_level"],
        "tool_policy": tool_policy or definition["tool_policy"],
        "is_default": False,
        "notes": row.get("notes") or "",
        "updated_by": row.get("updated_by") or "",
        "updated_at": row.get("updated_at"),
        "created_at": row.get("created_at"),
        "project": _project_summary(project),
    }


def _project_summary(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(project["id"]),
        "project_name": project["project_name"],
        "project_id": project["project_id"],
        "repo_path": str(project.get("repo_path") or ""),
    }


def _scan_repo(repo_path: str) -> dict[str, Any]:
    if not repo_path:
        return {"repo_path": "", "exists": False}
    try:
        path = Path(repo_path)
        return {"repo_path": repo_path, "exists": path.exists() and path.is_dir()}
    except Exception:
        return {"repo_path": repo_path, "exists": False}


def _scan_adc(repo_path: str) -> dict[str, Any]:
    root = Path(repo_path)
    present: list[str] = []
    missing: list[str] = []
    for relative in REQUIRED_ADC_FILES:
        target = root / relative
        if target.exists() and target.is_file():
            present.append(relative)
        else:
            missing.append(relative)
    return {
        "required_count": len(REQUIRED_ADC_FILES),
        "present_count": len(present),
        "present": present,
        "missing": missing,
    }


def _empty_adc_scan(repo_path: str) -> dict[str, Any]:
    return {
        "required_count": len(REQUIRED_ADC_FILES),
        "present_count": 0,
        "present": [],
        "missing": list(REQUIRED_ADC_FILES) if repo_path else [],
    }


def _scan_verification(repo_path: str) -> dict[str, Any]:
    root = Path(repo_path)
    test_paths = [relative for relative in TEST_PATHS if (root / relative).exists()]
    test_configs = [relative for relative in TEST_CONFIG_FILES if (root / relative).exists()]
    workflow_dir = root / ".github" / "workflows"
    workflows = []
    if workflow_dir.exists() and workflow_dir.is_dir():
        workflows = sorted(
            str(path.relative_to(root)).replace("\\", "/")
            for path in workflow_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".yml", ".yaml"}
        )
    return {
        "has_tests": bool(test_paths or test_configs),
        "test_paths": test_paths,
        "test_configs": test_configs,
        "ci_workflow_count": len(workflows),
        "ci_workflows": workflows,
    }


def _empty_verification_scan(repo_path: str) -> dict[str, Any]:
    return {
        "has_tests": False,
        "test_paths": [],
        "test_configs": [],
        "ci_workflow_count": 0,
        "ci_workflows": [],
    }


def _collect_graph_stats(registry: Any, project_name: str) -> dict[str, Any]:
    if registry is None:
        return {"available": False, "error": "graph registry unavailable"}
    try:
        graph = registry.get(project_name)
        stats = {key: _graph_count(graph, query) for key, query in GRAPH_COUNT_QUERIES.items()}
        stats["total_nodes"] = stats["repositories"] + stats["files"] + stats["symbols"] + stats["variables"]
        stats["total_edges"] = (
            stats["call_edges"]
            + stats["import_edges"]
            + stats["flow_edges"]
            + stats["defines_edges"]
            + stats["contains_edges"]
        )
        stats["available"] = True
        return stats
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def _graph_count(graph: Any, query: str) -> int:
    result = graph.query(query)
    rows = getattr(result, "result_set", None) or []
    if not rows or not rows[0]:
        return 0
    value = rows[0][0]
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def _latest_index_job(consumer: Any, repo_path: str) -> dict[str, Any] | None:
    if consumer is None or not repo_path:
        return None
    try:
        jobs = await consumer.get_jobs_by_repo(repo_path)
    except Exception:
        return None
    if not jobs:
        return None
    jobs.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return dict(jobs[0])


async def _safe_activity_count(work_briefing_service: WorkBriefingService, project_id: str) -> int:
    try:
        return int(await work_briefing_service.count_recent(project_id=project_id))
    except Exception:
        return 0


async def _safe_recent_activities(
    work_briefing_service: WorkBriefingService,
    project_id: str,
    limit: int,
) -> list[WorkActivity]:
    try:
        return await work_briefing_service.list_recent(project_id=project_id, limit=limit)
    except Exception:
        return []


async def _active_mcp_token_count(db: aiosqlite.Connection, project_db_id: int) -> int:
    async with db.execute(
        "SELECT count(*) AS cnt FROM project_tokens WHERE project_id = ? AND token_type = 'mcp' AND is_active = 1",
        (project_db_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return 0
    try:
        return int(row["cnt"])
    except (TypeError, ValueError, KeyError):
        return 0


def _coverage_status(present_count: int, required_count: int) -> str:
    if required_count <= 0:
        return "unknown"
    if present_count == required_count:
        return "ok"
    if present_count > 0:
        return "warn"
    return "fail"


def _graph_status(stats: dict[str, Any]) -> str:
    if not stats.get("available"):
        return "unknown"
    if int(stats.get("files") or 0) > 0 and int(stats.get("symbols") or 0) > 0:
        return "ok"
    if int(stats.get("files") or 0) > 0:
        return "warn"
    return "fail"


def _graph_summary(stats: dict[str, Any]) -> str:
    if not stats.get("available"):
        return f"Graph stats unavailable: {stats.get('error') or 'unknown error'}"
    return f"{stats.get('files', 0)} files, {stats.get('symbols', 0)} symbols, {stats.get('call_edges', 0)} call edges"


def _index_job_status(job: dict[str, Any] | None) -> str:
    if not job:
        return "unknown"
    status = str(job.get("status") or "").lower()
    if status == "done":
        return "ok"
    if status in {"pending", "processing"}:
        return "warn"
    if status in {"failed", "stale"}:
        return "fail"
    return "unknown"


def _index_job_summary(job: dict[str, Any] | None) -> str:
    if not job:
        return "No recent index job status found"
    status = str(job.get("status") or "unknown")
    job_id = str(job.get("job_id") or "unknown")
    updated = str(job.get("updated_at") or "unknown time")
    return f"Latest job {job_id} is {status}, updated {updated}"


def _signal(
    key: str,
    label: str,
    status: str,
    summary: str,
    *,
    evidence: Any = None,
    recommendation: str | None = None,
) -> dict[str, Any]:
    payload = {
        "key": key,
        "label": label,
        "status": status,
        "summary": summary,
        "evidence": evidence if evidence is not None else {},
    }
    if recommendation:
        payload["recommendation"] = recommendation
    return payload


def _dimension(key: str, label: str, signals: list[dict[str, Any]]) -> dict[str, Any]:
    known_scores = [_points_for_status(signal["status"]) for signal in signals if _points_for_status(signal["status"]) is not None]
    score = _average_known_scores(known_scores)
    return {
        "key": key,
        "label": label,
        "score": score,
        "status": _status_from_score(score),
        "signals": signals,
    }


def _points_for_status(status: str) -> float | None:
    if status == "ok":
        return 100.0
    if status == "warn":
        return 50.0
    if status == "fail":
        return 0.0
    return None


def _average_known_scores(scores: list[float | int | None]) -> int | None:
    known = [float(score) for score in scores if score is not None]
    if not known:
        return None
    return int(round(sum(known) / len(known)))


def _status_from_score(score: int | None) -> str:
    if score is None:
        return "unknown"
    if score >= 80:
        return "ok"
    if score >= 50:
        return "warn"
    return "fail"


def _recommended_next_actions(dimensions: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    seen: set[str] = set()
    for dimension in dimensions:
        for signal in dimension.get("signals", []):
            recommendation = signal.get("recommendation")
            if not recommendation or recommendation in seen:
                continue
            if signal.get("status") == "ok":
                continue
            actions.append(recommendation)
            seen.add(recommendation)
    return actions[:8]


def _evidence_summary_from_row(
    row: dict[str, Any],
    *,
    correlation: dict[str, Any] | None = None,
    pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parsed_correlation = correlation
    if parsed_correlation is None:
        try:
            parsed_correlation = json.loads(row.get("correlation_json") or "{}")
        except Exception:
            parsed_correlation = {}
    readiness = (pack or {}).get("readiness") if isinstance(pack, dict) else {}
    return {
        "id": int(row["id"]),
        "evidence_id": row["evidence_id"],
        "project_db_id": row.get("project_id"),
        "project_id": row.get("project_external_id") or "",
        "project_name": row.get("project_name") or "",
        "status": row.get("status") or "generated",
        "correlation": parsed_correlation or {},
        "readiness_status": readiness.get("overall_status") if isinstance(readiness, dict) else None,
        "readiness_score": readiness.get("overall_score") if isinstance(readiness, dict) else None,
        "created_by": row.get("created_by") or "",
        "created_at": row.get("created_at") or "",
    }


def _sanitize_activity(activity: WorkActivity) -> dict[str, Any]:
    metadata_keys = sorted(str(key) for key in activity.raw_metadata.keys()) if isinstance(activity.raw_metadata, dict) else []
    return {
        "activity_id": activity.activity_id,
        "source_item_id": activity.source_item_id,
        "project_id": activity.project_id,
        "event_type": activity.event_type,
        "title": activity.title,
        "summary": activity.summary,
        "status": activity.status,
        "priority": activity.priority,
        "owner": activity.owner,
        "source_url": activity.source_url,
        "tags": list(activity.tags),
        "occurred_at": activity.occurred_at.isoformat().replace("+00:00", "Z"),
        "synced_at": activity.synced_at.isoformat().replace("+00:00", "Z"),
        "metadata_keys": metadata_keys,
    }


def _render_evidence_markdown(pack: dict[str, Any]) -> str:
    project = pack["project"]
    readiness = pack["readiness"]
    activities = pack["activity_evidence"]["activities"]
    correlation = pack.get("correlation", {})
    policy_gates = pack.get("policy_gates", {})
    signal_evidence = pack.get("signal_evidence", {})
    trace_evidence = pack.get("trace_evidence", {})
    lines = [
        f"# AI-First Evidence Pack - {project['project_name']}",
        "",
        f"Generated: {pack['generated_at']}",
        f"Project ID: {project['project_id']}",
        f"Correlation mode: {correlation.get('mode', 'project_recent')}",
        f"Policy profile: {pack['policy_profile']['name']} ({pack['policy_profile']['enforcement_level']})",
    ]
    filters = correlation.get("filters") or {}
    if filters:
        lines.extend(["", "## Correlation", ""])
        for key, value in filters.items():
            lines.append(f"- {key}: {value}")
        lines.append(f"- Matched activities: {correlation.get('activity_match_count', 0)}")
        lines.append(f"- Matched traces: {correlation.get('trace_match_count', 0)}")
    lines.extend([
        "",
        "## Readiness",
        "",
        f"Overall: {readiness['overall_status']} ({readiness['overall_score'] if readiness['overall_score'] is not None else 'unknown'})",
    ])
    for dimension in readiness["dimensions"]:
        score = dimension["score"] if dimension["score"] is not None else "unknown"
        lines.append(f"- {dimension['label']}: {dimension['status']} ({score})")
    gates = policy_gates.get("gates") or []
    lines.extend(["", "## Policy Gates", ""])
    if not gates:
        lines.append(f"No active gates for profile {policy_gates.get('profile_name', 'observe-only')}.")
    else:
        lines.append(f"Overall: {policy_gates.get('overall_status', 'unknown')} ({policy_gates.get('enforcement_level', 'L0')})")
        for gate in gates:
            lines.append(f"- {gate['label']} [{gate['status']}/{gate['severity']}]: {gate['summary']}")
    if readiness["recommended_next_actions"]:
        lines.extend(["", "## Recommended Next Actions", ""])
        lines.extend(f"- {action}" for action in readiness["recommended_next_actions"])
    lines.extend(["", "## Recent Activity Evidence", ""])
    if not activities:
        lines.append("No recent work briefing activity was available for this project.")
    else:
        for activity in activities[:10]:
            status = f" [{activity['status']}]" if activity.get("status") else ""
            source = f" ({activity['source_item_id']})" if activity.get("source_item_id") else ""
            lines.append(f"- {activity['occurred_at']} {activity['event_type']}{status}{source}: {activity['title']}")
    signals = signal_evidence.get("signals") or []
    lines.extend(["", "## Signal Evidence", ""])
    if not signals:
        lines.append("No matching PR, CI, or benchmark signals were exported.")
    else:
        for signal in signals[:10]:
            value = f" - {signal.get('value')} {signal.get('unit') or ''}" if signal.get("value") else ""
            lines.append(
                f"- {signal.get('observed_at') or 'unknown time'} {signal.get('signal_type')} "
                f"[{signal.get('status')}]: {signal.get('name')}{value}"
            )
    traces = trace_evidence.get("traces") or []
    lines.extend(["", "## MCP Trace Evidence", ""])
    if not traces:
        lines.append(f"No matching MCP traces were exported. Trace status: {trace_evidence.get('status', 'unknown')}.")
    else:
        for trace in traces[:10]:
            lines.append(
                f"- {trace.get('timestamp') or 'unknown time'} {trace.get('tool')} "
                f"({trace.get('latency_ms')} ms, {trace.get('result_count')} results): {trace.get('trace_id')}"
            )
    return "\n".join(lines)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
