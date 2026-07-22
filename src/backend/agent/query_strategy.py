"""CG-first query strategy for agents.

Strategy:
1. Query ContextGraph MCP first (retrieve_context + call graph).
2. Score graph context quality under a token budget.
3. Fallback to local code snippets only when graph quality is insufficient.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client


def estimate_tokens(text: str) -> int:
    """Rough token estimate for budget control (4 chars ~= 1 token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def trim_items_to_budget(
    items: list[dict[str, Any]],
    budget_tokens: int,
) -> tuple[list[dict[str, Any]], int]:
    """Keep items in order while respecting token budget."""
    kept: list[dict[str, Any]] = []
    used = 0
    for item in items:
        item_tokens = estimate_tokens(json.dumps(item, ensure_ascii=True))
        if used + item_tokens > budget_tokens:
            break
        kept.append(item)
        used += item_tokens
    return kept, used


def read_code_snippet(
    repo_root: Path,
    relative_path: str,
    line_start: int,
    line_end: int,
    context_lines: int = 3,
    max_chars: int = 1200,
) -> str:
    """Read a bounded code snippet around a symbol location."""
    file_path = (repo_root / relative_path).resolve()
    if not file_path.exists() or not file_path.is_file():
        return ""

    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""

    if not lines:
        return ""

    start_idx = max(0, line_start - 1 - context_lines)
    end_idx = min(len(lines), line_end + context_lines)
    snippet = "\n".join(lines[start_idx:end_idx]).strip()
    if len(snippet) > max_chars:
        return snippet[: max_chars - 3] + "..."
    return snippet


def _query_terms(query: str) -> set[str]:
    return {part.strip().lower() for part in query.split() if part.strip()}


def score_graph_item(item: dict[str, Any], query: str) -> float:
    """Score one graph item based on query match and richness."""
    terms = _query_terms(query)
    qualified_name = str(item.get("qualified_name") or "").lower()
    summary = str(item.get("summary") or "").lower()
    snippet = str(item.get("snippet") or "")
    callers = item.get("callers") or []
    callees = item.get("callees") or []

    if not terms:
        return 0.0

    matched = sum(1 for term in terms if term in qualified_name or term in summary)
    match_score = matched / max(1, len(terms))
    snippet_score = 0.2 if snippet else 0.0
    relation_score = min(0.2, 0.05 * (len(callers) + len(callees)))
    symbol_score = 0.1 if item.get("symbol_type") else 0.0
    return min(1.0, match_score * 0.5 + snippet_score + relation_score + symbol_score)


def evaluate_graph_quality(items: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """Aggregate quality score for trimmed graph context."""
    if not items:
        return {
            "quality_score": 0.0,
            "avg_item_score": 0.0,
            "matched_items": 0,
            "has_snippets": False,
        }

    scores = [score_graph_item(item, query) for item in items]
    matched_items = sum(1 for score in scores if score >= 0.45)
    has_snippets = any(bool(item.get("snippet")) for item in items)
    avg_item_score = sum(scores) / len(scores)
    breadth_bonus = min(0.15, 0.03 * len(items))
    snippet_bonus = 0.1 if has_snippets else 0.0
    quality_score = min(1.0, avg_item_score + breadth_bonus + snippet_bonus)
    return {
        "quality_score": round(quality_score, 3),
        "avg_item_score": round(avg_item_score, 3),
        "matched_items": matched_items,
        "has_snippets": has_snippets,
    }


def decide_fallback(
    trimmed_items: list[dict[str, Any]],
    query: str,
    min_graph_hits: int,
    quality_threshold: float,
) -> tuple[bool, str, dict[str, Any]]:
    """Decide whether local fallback is needed."""
    quality = evaluate_graph_quality(trimmed_items, query)
    if len(trimmed_items) < min_graph_hits:
        return True, "insufficient_graph_hits", quality
    if quality["quality_score"] < quality_threshold:
        return True, "low_graph_quality", quality
    return False, "graph_context_sufficient", quality


@dataclass
class StrategyConfig:
    repo_root: Path
    base_url: str | None = None
    graph_top_k: int = 8
    min_graph_hits: int = 3
    token_budget: int = 1800
    relation_depth: int = 1
    include_relations: bool = True
    fallback_max_files: int = 3
    fallback_context_lines: int = 3
    quality_threshold: float = 0.55
    mcp_token: str | None = None
    project_id: str | None = None


def _auth_headers(token: str | None, project_id: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    token = token or os.getenv("CONTEXTGRAPH_MCP_TOKEN")
    project_id = project_id or os.getenv("CONTEXTGRAPH_PROJECT_ID")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if project_id:
        headers["X-Project-ID"] = project_id
    return headers


def _decode_call_tool_result(result: Any) -> Any:
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        first = content[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except Exception:
                return text
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    return result


def _build_local_fallback_snippets(
    query: str,
    repo_root: Path,
    graph_hits: list[dict[str, Any]],
    fallback_max_files: int,
    fallback_context_lines: int,
) -> list[dict[str, Any]]:
    fallback: list[dict[str, Any]] = []

    for hit in graph_hits[:fallback_max_files]:
        file_path = str(hit.get("file_path") or "")
        if not file_path:
            continue
        snippet = read_code_snippet(
            repo_root,
            file_path,
            int(hit.get("line_start") or 1),
            int(hit.get("line_end") or 1),
            context_lines=fallback_context_lines,
        )
        if snippet:
            fallback.append(
                {
                    "source": "symbol-snippet",
                    "file_path": file_path,
                    "line_start": int(hit.get("line_start") or 1),
                    "line_end": int(hit.get("line_end") or 1),
                    "snippet": snippet,
                }
            )

    if fallback:
        return fallback

    needle = query.strip().lower()
    if not needle:
        return fallback

    scanned = 0
    for pattern in ("*.py", "*.ts", "*.tsx", "*.js", "*.jsx"):
        for path in repo_root.rglob(pattern):
            scanned += 1
            if scanned > 300:
                return fallback
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            idx = text.lower().find(needle)
            if idx < 0:
                continue

            line_start = text[:idx].count("\n") + 1
            line_end = min(line_start + 20, line_start + text[idx:].count("\n"))
            rel = str(path.relative_to(repo_root)).replace("\\", "/")
            snippet = read_code_snippet(
                repo_root,
                rel,
                line_start,
                line_end,
                context_lines=fallback_context_lines,
            )
            if snippet:
                fallback.append(
                    {
                        "source": "keyword-fallback",
                        "file_path": rel,
                        "line_start": line_start,
                        "line_end": line_end,
                        "snippet": snippet,
                    }
                )
            if len(fallback) >= fallback_max_files:
                return fallback
    return fallback


def run_cg_first_strategy(
    query: str,
    repo_root: Path,
    retrieve_graph_hits,
    get_call_graph,
    graph_top_k: int = 8,
    min_graph_hits: int = 3,
    token_budget: int = 1800,
    relation_depth: int = 1,
    include_relations: bool = True,
    fallback_max_files: int = 3,
    fallback_context_lines: int = 3,
    quality_threshold: float = 0.55,
    source_label: str = "contextgraph-mcp",
) -> dict[str, Any]:
    """Run CG-first strategy using injected graph retrieval callables."""
    graph_hits_raw = retrieve_graph_hits(query=query, limit=graph_top_k)
    graph_hits = [item for item in graph_hits_raw if isinstance(item, dict)]

    graph_items: list[dict[str, Any]] = []
    for hit in graph_hits:
        qname = str(hit.get("qualified_name") or "")
        item = dict(hit)
        item.setdefault("qualified_name", qname)
        item.setdefault("symbol_type", hit.get("symbol_type"))
        item.setdefault("file_path", hit.get("file_path"))
        item.setdefault("line_start", hit.get("line_start"))
        item.setdefault("line_end", hit.get("line_end"))
        inline_relations_present = bool(item.get("callers") or item.get("callees"))
        if include_relations and qname and not inline_relations_present:
            try:
                graph = get_call_graph(qualified_name=qname, depth=relation_depth)
                if isinstance(graph, dict):
                    item["callers"] = graph.get("callers", [])
                    item["callees"] = graph.get("callees", [])
            except Exception as exc:
                item["relation_error"] = str(exc)
        graph_items.append(item)

    graph_items_trimmed, graph_tokens = trim_items_to_budget(graph_items, token_budget)
    used_fallback, fallback_reason, quality = decide_fallback(
        trimmed_items=graph_items_trimmed,
        query=query,
        min_graph_hits=min_graph_hits,
        quality_threshold=quality_threshold,
    )

    fallback_items: list[dict[str, Any]] = []
    fallback_tokens = 0
    if used_fallback:
        remaining = max(0, token_budget - graph_tokens)
        fallback_items = _build_local_fallback_snippets(
            query=query,
            repo_root=repo_root,
            graph_hits=graph_hits,
            fallback_max_files=fallback_max_files,
            fallback_context_lines=fallback_context_lines,
        )
        fallback_items, fallback_tokens = trim_items_to_budget(fallback_items, remaining)

    return {
        "strategy": "cg-first",
        "query": query,
        "source": source_label,
        "token_budget": token_budget,
        "estimated_tokens": graph_tokens + fallback_tokens,
        "graph_hits_total": len(graph_hits),
        "graph_context": graph_items_trimmed,
        "quality_score": quality["quality_score"],
        "avg_item_score": quality["avg_item_score"],
        "matched_items": quality["matched_items"],
        "quality_threshold": quality_threshold,
        "fallback_reason": fallback_reason,
        "used_fallback": used_fallback,
        "fallback_context": fallback_items,
    }


class CGFirstQueryStrategy:
    """ContextGraph-first query strategy with safe local fallback."""

    def __init__(self, config: StrategyConfig) -> None:
        self._cfg = config
        self._headers = _auth_headers(config.mcp_token, config.project_id)

    def run(self, query: str) -> dict[str, Any]:
        return asyncio.run(self.run_async(query))

    def _read_message_endpoint(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/mcp/sse"

    async def run_async(self, query: str) -> dict[str, Any]:
        if not self._cfg.base_url:
            raise RuntimeError("StrategyConfig.base_url is required for CGFirstQueryStrategy")

        async with sse_client(
            self._read_message_endpoint(self._cfg.base_url),
            headers=self._headers,
            timeout=20,
            sse_read_timeout=60,
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                graph_hits = await self._retrieve_graph_hits(session, query, self._cfg.graph_top_k)
                call_graphs: dict[str, dict[str, Any]] = {}
                if self._cfg.include_relations:
                    for hit in graph_hits:
                        qualified_name = str(hit.get("qualified_name") or "")
                        if not qualified_name or qualified_name in call_graphs:
                            continue
                        call_graphs[qualified_name] = await self._get_call_graph(
                            session,
                            qualified_name,
                            self._cfg.relation_depth,
                        )

        return run_cg_first_strategy(
            query=query,
            repo_root=self._cfg.repo_root,
            retrieve_graph_hits=lambda query, limit: graph_hits[:limit],
            get_call_graph=lambda qualified_name, depth: call_graphs.get(qualified_name, {}),
            graph_top_k=self._cfg.graph_top_k,
            min_graph_hits=self._cfg.min_graph_hits,
            token_budget=self._cfg.token_budget,
            relation_depth=self._cfg.relation_depth,
            include_relations=self._cfg.include_relations,
            fallback_max_files=self._cfg.fallback_max_files,
            fallback_context_lines=self._cfg.fallback_context_lines,
            quality_threshold=self._cfg.quality_threshold,
            source_label="contextgraph-client",
        )

    async def _tool_call(self, session: ClientSession, name: str, arguments: dict[str, Any]) -> Any:
        result = await session.call_tool(name, arguments)
        return _decode_call_tool_result(result)

    async def _retrieve_graph_hits(self, session: ClientSession, query: str, limit: int) -> list[dict[str, Any]]:
        payload = await self._tool_call(session, "retrieve_context", {"query": query, "limit": limit})
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    async def _get_call_graph(self, session: ClientSession, qualified_name: str, depth: int) -> dict[str, Any]:
        payload = await self._tool_call(
            session,
            "find_call_graph",
            {"qualified_name": qualified_name, "depth": depth},
        )
        if isinstance(payload, dict):
            return payload
        return {}
