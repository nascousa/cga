"""Deterministic routed-query decisions and bounded, identity-free diagnostics."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import threading
from typing import Literal

from backend.runtime_config import QueryMode, QueryPolicy

QueryIntent = Literal["lookup", "impact", "audit"]
_recent: deque[dict] = deque(maxlen=100)
_lock = threading.Lock()


def select_query_tool(
    policy: QueryPolicy, relation: str, direction: str,
    intent: QueryIntent, requires_evidence: bool,
) -> tuple[str, str]:
    if intent not in ("lookup", "impact", "audit"):
        raise ValueError("intent must be lookup, impact or audit")
    if type(requires_evidence) is not bool:
        raise ValueError("requires_evidence must be a boolean")
    if intent == "audit":
        return "query_graph_relation", "audit_requires_full_evidence"
    if requires_evidence:
        return "query_graph_relation", "caller_requires_evidence"
    if policy.mode == QueryMode.EVIDENCE_FIRST:
        return "query_graph_relation", "admin_evidence_first"
    if policy.mode == QueryMode.AUTO and intent == "impact":
        return "query_graph_relation", "auto_impact_requires_evidence"
    legacy = {
        ("CALLS", "outbound"): "find_callees",
        ("CALLS", "inbound"): "find_callers",
        ("IMPORTS", "outbound"): "get_file_imports",
        ("IMPORTS", "inbound"): "get_file_dependents",
    }.get((relation, direction))
    if legacy:
        return legacy, "auto_simple_lookup" if policy.mode == QueryMode.AUTO else "admin_lightweight_first"
    return "query_graph_relation", "no_equivalent_lightweight_tool"


def encoded_response_bytes(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def fit_query_response(payload: dict, budget: int) -> dict:
    """Budget the canonical tool result, including its self-describing byte count."""
    decision = payload["decision"]
    result = payload["result"]
    items = result["edges"] if isinstance(result, dict) else result
    decision["budget_omitted_items"] = 0
    while True:
        decision["returned_items"] = len(items)
        decision["response_bytes"] = 0
        while True:
            size = encoded_response_bytes(payload)
            if size == decision["response_bytes"]:
                break
            decision["response_bytes"] = size
        if size <= budget:
            return payload
        if not items:
            raise ValueError(
                "Response budget cannot fit required metadata; increase max_response_bytes"
            )
        items.pop()
        decision["budget_omitted_items"] += 1
        decision["truncated"] = True
        decision["has_more"] = True
        decision["truncation_reason"] = "byte_budget"
        if isinstance(result, dict):
            result["has_more"] = True
            if not items:
                result["status"] = "budget_exhausted"
        if not items:
            decision["status"] = "budget_exhausted"


def record_query_decision(project_id: str, decision: dict) -> None:
    record = {
        **decision, "project_id": project_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        _recent.appendleft(record)


def recent_query_decisions() -> dict:
    with _lock:
        records = [dict(record) for record in _recent]
    return {"scope": "current_process", "capacity": 100, "records": records}
