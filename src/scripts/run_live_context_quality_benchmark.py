#!/usr/bin/env python
"""Run live database-backed ContextGraph HPS benchmarks.

The regular context-quality benchmark consumes a prepared JSONL manifest.
This script builds that manifest from the currently running CGA runtime:

* PostgreSQL ``projects`` table selects real registered projects.
* FalkorDB project graphs select real indexed symbols.
* Local repository files provide real source text for baseline and CG context.

Each selected project contributes a fixed number of deterministic symbol-level
cases, then the script reports per-project averages and an overall average.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
from falkordb import FalkorDB

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from backend.perf.context_quality import benchmark_context_quality  # noqa: E402


DEFAULT_POSTGRES_DSN = "postgresql://app:app@localhost:15432/appdb"
DEFAULT_FALKORDB_HOST = "localhost"
DEFAULT_FALKORDB_PORT = 16381
DEFAULT_REPOS_HOST_ROOT = Path("D:/Repos")
DEFAULT_CASES_PER_PROJECT = 34
DEFAULT_PROJECTS = ["BrowserAgent", "IcM_Automation", "ADC"]
MAX_GRAPH_SYMBOLS = 2000
MAX_BASELINE_CHARS = 16000
MAX_NOISE_CHARS = 8000
CG_CONTEXT_RADIUS_LINES = 4


@dataclass(frozen=True)
class ProjectRecord:
    project_name: str
    project_id: str
    repo_path: str
    graph_name: str
    host_repo_path: Path


@dataclass(frozen=True)
class SymbolRecord:
    qualified_name: str
    name: str
    graph_file_path: str
    host_file_path: Path
    line_start: int
    line_end: int


def _graph_name(project_name: str) -> str:
    return project_name.strip().lower()


def _resolve_host_repo_path(project_name: str, repo_path: str, repos_host_root: Path) -> Path:
    if repo_path:
        raw = repo_path.replace("\\", "/")
        if raw.startswith("/repos/"):
            return repos_host_root / raw.removeprefix("/repos/")
        return Path(repo_path)
    return repos_host_root / project_name


def _resolve_host_file_path(graph_file_path: str, project: ProjectRecord, repos_host_root: Path) -> Path:
    normalized = graph_file_path.replace("\\", "/")
    prefix = f"/repos/{project.project_name}/"
    if normalized.startswith(prefix):
        return project.host_repo_path / normalized.removeprefix(prefix)
    if normalized.startswith("/repos/"):
        return repos_host_root / normalized.removeprefix("/repos/")
    candidate = Path(graph_file_path)
    if candidate.is_absolute():
        return candidate
    return project.host_repo_path / graph_file_path


async def load_active_projects(postgres_dsn: str, project_names: list[str], repos_host_root: Path) -> list[ProjectRecord]:
    conn = await asyncpg.connect(postgres_dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT project_name, project_id, repo_path
            FROM projects
            WHERE is_active = 1
            ORDER BY id
            """
        )
    finally:
        await conn.close()

    requested = {name.lower(): name for name in project_names}
    projects: list[ProjectRecord] = []
    for row in rows:
        name = str(row["project_name"])
        if requested and name.lower() not in requested:
            continue
        repo_path = str(row["repo_path"] or "")
        projects.append(
            ProjectRecord(
                project_name=name,
                project_id=str(row["project_id"]),
                repo_path=repo_path,
                graph_name=_graph_name(name),
                host_repo_path=_resolve_host_repo_path(name, repo_path, repos_host_root),
            )
        )

    missing = sorted(set(requested) - {project.project_name.lower() for project in projects})
    if missing:
        raise SystemExit(f"Requested active project(s) not found in the live database: {', '.join(missing)}")
    return projects


def load_graph_symbols(project: ProjectRecord, graph_client: FalkorDB, repos_host_root: Path) -> list[SymbolRecord]:
    graph = graph_client.select_graph(project.graph_name)
    result = graph.query(
        """
        MATCH (s:Symbol)
        WHERE s.line_start IS NOT NULL
          AND s.line_end IS NOT NULL
          AND s.file_path IS NOT NULL
        RETURN s.qualified_name, s.name, s.file_path, s.line_start, s.line_end
        ORDER BY s.file_path, s.line_start, s.qualified_name
        LIMIT $limit
        """,
        {"limit": MAX_GRAPH_SYMBOLS},
    )

    symbols: list[SymbolRecord] = []
    for row in result.result_set:
        qualified_name, name, file_path, line_start, line_end = row
        host_file_path = _resolve_host_file_path(str(file_path), project, repos_host_root)
        symbols.append(
            SymbolRecord(
                qualified_name=str(qualified_name),
                name=str(name),
                graph_file_path=str(file_path),
                host_file_path=host_file_path,
                line_start=int(line_start or 1),
                line_end=int(line_end or line_start or 1),
            )
        )
    return symbols


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _line_excerpt(text: str, line_start: int, line_end: int, radius: int = CG_CONTEXT_RADIUS_LINES) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    start = max(1, line_start - radius)
    end = min(len(lines), max(line_end, line_start) + radius)
    excerpt = lines[start - 1 : end]
    return "\n".join(excerpt)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _select_evenly(items: list[SymbolRecord], count: int) -> list[SymbolRecord]:
    if len(items) <= count:
        return list(items)
    if count <= 1:
        return [items[0]]
    selected: list[SymbolRecord] = []
    used: set[int] = set()
    span = len(items) - 1
    for index in range(count):
        raw = round(index * span / (count - 1))
        while raw in used and raw < len(items) - 1:
            raw += 1
        while raw in used and raw > 0:
            raw -= 1
        used.add(raw)
        selected.append(items[raw])
    return selected


def _valid_symbols(symbols: list[SymbolRecord]) -> list[SymbolRecord]:
    valid: list[SymbolRecord] = []
    for symbol in symbols:
        if symbol.line_start < 1 or symbol.line_end < 1:
            continue
        if not symbol.host_file_path.exists() or not symbol.host_file_path.is_file():
            continue
        try:
            text = _read_text(symbol.host_file_path)
        except OSError:
            continue
        if not _line_excerpt(text, symbol.line_start, symbol.line_end).strip():
            continue
        valid.append(symbol)
    return valid


def build_cases(project: ProjectRecord, symbols: list[SymbolRecord], cases_per_project: int) -> list[dict[str, Any]]:
    valid = _valid_symbols(symbols)
    if len(valid) < cases_per_project:
        raise SystemExit(
            f"Project {project.project_name} has {len(valid)} valid symbol candidates; "
            f"{cases_per_project} are required."
        )

    selected = _select_evenly(valid, cases_per_project)
    file_pool = []
    seen_files: set[Path] = set()
    for symbol in valid:
        if symbol.host_file_path not in seen_files:
            seen_files.add(symbol.host_file_path)
            file_pool.append(symbol.host_file_path)

    cases: list[dict[str, Any]] = []
    for index, symbol in enumerate(selected, start=1):
        source_text = _read_text(symbol.host_file_path)
        gold_item = f"file:{symbol.graph_file_path}#{symbol.name}"
        target_text = _truncate(source_text, MAX_BASELINE_CHARS)
        cg_text = _line_excerpt(source_text, symbol.line_start, symbol.line_end)
        neighbor = valid[(valid.index(symbol) + 1) % len(valid)]
        neighbor_text = _line_excerpt(
            _read_text(neighbor.host_file_path),
            neighbor.line_start,
            neighbor.line_end,
            radius=2,
        )

        noise_chunks: list[dict[str, Any]] = []
        for noise_path in file_pool:
            if noise_path == symbol.host_file_path:
                continue
            noise_chunks.append(
                {
                    "id": f"noise-{len(noise_chunks) + 1}",
                    "text": _truncate(_read_text(noise_path), MAX_NOISE_CHARS),
                    "evidence": [],
                    "symbols": [],
                }
            )
            if len(noise_chunks) == 2:
                break

        cases.append(
            {
                "id": f"{project.graph_name}-live-{index:02d}",
                "project": project.project_name,
                "category": "live-database-symbol-context",
                "query": f"Where is {symbol.name} implemented and what local source context supports it?",
                "goldItems": [gold_item],
                "targetSymbols": [symbol.qualified_name],
                "baseline": {
                    "chunks": [
                        {
                            "id": "broad-target-file",
                            "text": target_text,
                            "evidence": [gold_item],
                            "symbols": [symbol.qualified_name],
                        },
                        *noise_chunks,
                    ],
                    "symbols": [symbol.qualified_name],
                },
                "cg": {
                    "chunks": [
                        {
                            "id": "symbol-local-context",
                            "text": cg_text,
                            "evidence": [gold_item],
                            "symbols": [symbol.qualified_name],
                            "sourceFile": symbol.graph_file_path,
                            "lineRange": [symbol.line_start, symbol.line_end],
                        },
                        {
                            "id": "graph-neighbor-context",
                            "text": neighbor_text,
                            "evidence": [],
                            "symbols": [neighbor.qualified_name],
                            "sourceFile": neighbor.graph_file_path,
                            "lineRange": [neighbor.line_start, neighbor.line_end],
                        },
                    ],
                    "symbols": [symbol.qualified_name, neighbor.qualified_name],
                },
            }
        )
    return cases


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def summarize_projects(report: dict[str, Any]) -> list[dict[str, Any]]:
    by_project: dict[str, list[dict[str, Any]]] = {}
    for item in report.get("cases", []):
        by_project.setdefault(str(item.get("project") or ""), []).append(item)

    summaries: list[dict[str, Any]] = []
    for project_name in sorted(by_project):
        items = by_project[project_name]
        summaries.append(
            {
                "project": project_name,
                "caseCount": len(items),
                "avgBaselineHps": _average([item["baseline"]["hallucinationPressureScore"] for item in items]),
                "avgCgHps": _average([item["cg"]["hallucinationPressureScore"] for item in items]),
                "avgHpsReductionPercent": _average([item["comparison"]["hpsReductionPercent"] for item in items]),
                "avgBaselineTokens": _average([item["baseline"]["totalTokens"] for item in items]),
                "avgCgTokens": _average([item["cg"]["totalTokens"] for item in items]),
                "avgTokenReductionPercent": _average([item["comparison"]["tokenReductionPercent"] for item in items]),
            }
        )
    return summaries


def render_markdown(report: dict[str, Any]) -> str:
    metadata = report["liveBenchmark"]
    summary = report["summary"]
    project_names = " ".join(project["project"] for project in metadata["projects"])
    lines = [
        "# ContextGraph Live Project HPS Benchmark",
        "",
        f"Run date: {metadata['runDate']}",
        f"Method: `{report['method']}`",
        f"Cases per project: {metadata['casesPerProject']}",
        f"Total cases: {summary['caseCount']}",
        "",
        "## Methodology",
        "",
        "This benchmark uses the currently running CGA runtime instead of a hand-written sample manifest.",
        "It selects active projects from the PostgreSQL `projects` table, reads indexed symbols from each FalkorDB project graph, and extracts context from real local repository files.",
        "For each project, 34 deterministic symbol-level cases compare broad source context against graph-scoped context made from the target symbol excerpt plus one neighboring graph excerpt.",
        "HPS is a deterministic pre-answer context risk score; lower is better.",
        "",
        "## Selected Projects",
        "",
        "| Project | Graph | Valid Symbol Candidates | Cases |",
        "|---|---|---:|---:|",
    ]

    for project in metadata["projects"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    project["project"],
                    project["graph"],
                    str(project["validSymbolCandidates"]),
                    str(project["cases"]),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Results",
            "",
            "| Project | Cases | Baseline HPS | CG HPS | HPS Reduction | Baseline Tokens | CG Tokens | Token Reduction |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for project in report["projectSummary"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    project["project"],
                    str(project["caseCount"]),
                    str(project["avgBaselineHps"]),
                    str(project["avgCgHps"]),
                    f"{project['avgHpsReductionPercent']}%",
                    str(project["avgBaselineTokens"]),
                    str(project["avgCgTokens"]),
                    f"{project['avgTokenReductionPercent']}%",
                ]
            )
            + " |"
        )

    lines.append(
        "| **Average** | "
        + " | ".join(
            [
                f"**{summary['caseCount']}**",
                f"**{summary['avgBaselineHps']}**",
                f"**{summary['avgCgHps']}**",
                f"**{summary['avgHpsReductionPercent']}%**",
                f"**{summary['avgBaselineTokens']}**",
                f"**{summary['avgCgTokens']}**",
                f"**{summary['avgTokenReductionPercent']}%**",
            ]
        )
        + " |"
    )

    lines.extend(
        [
            "",
            "## Reproduce",
            "",
            "```powershell",
            "python -m src.scripts.run_live_context_quality_benchmark `",
            f"  --projects {project_names} `",
            f"  --cases-per-project {metadata['casesPerProject']} `",
            "  --output docs/benchmarks/context-quality-live-projects.report.json `",
            "  --markdown docs/benchmarks/context-quality-live-projects.report.md `",
            f"  --run-date {metadata['runDate']}",
            "```",
            "",
            "The JSON report contains the full per-case scoring output and is intended as a local artifact.",
            "",
        ]
    )
    return "\n".join(lines)


async def build_report(args: argparse.Namespace) -> dict[str, Any]:
    repos_host_root = Path(args.repos_host_root)
    projects = await load_active_projects(args.postgres_dsn, args.projects, repos_host_root)
    graph_client = FalkorDB(host=args.falkordb_host, port=args.falkordb_port)

    all_cases: list[dict[str, Any]] = []
    project_metadata: list[dict[str, Any]] = []
    for project in projects:
        symbols = load_graph_symbols(project, graph_client, repos_host_root)
        valid = _valid_symbols(symbols)
        if len(valid) < args.cases_per_project:
            continue
        cases = build_cases(project, valid, args.cases_per_project)
        all_cases.extend(cases)
        project_metadata.append(
            {
                "project": project.project_name,
                "projectId": project.project_id,
                "graph": project.graph_name,
                "repoPath": project.repo_path,
                "hostRepoPath": str(project.host_repo_path),
                "indexedSymbols": len(symbols),
                "validSymbolCandidates": len(valid),
                "cases": len(cases),
            }
        )

    if len(project_metadata) < args.min_projects:
        raise SystemExit(
            f"Only {len(project_metadata)} project(s) had enough valid graph-backed source symbols; "
            f"{args.min_projects} are required."
        )

    report = benchmark_context_quality(payload={"cases": all_cases}, repo_root=Path.cwd())
    report["liveBenchmark"] = {
        "runDate": args.run_date,
        "projectSource": "live PostgreSQL projects table",
        "graphSource": f"FalkorDB {args.falkordb_host}:{args.falkordb_port}",
        "casesPerProject": args.cases_per_project,
        "minProjects": args.min_projects,
        "projects": project_metadata,
    }
    report["projectSummary"] = summarize_projects(report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live database-backed ContextGraph HPS benchmarks")
    parser.add_argument("--postgres-dsn", default=os.getenv("CGA_POSTGRES_DSN", DEFAULT_POSTGRES_DSN))
    parser.add_argument("--falkordb-host", default=os.getenv("FALKORDB_HOST", DEFAULT_FALKORDB_HOST))
    parser.add_argument("--falkordb-port", type=int, default=int(os.getenv("FALKORDB_PORT", str(DEFAULT_FALKORDB_PORT))))
    parser.add_argument("--repos-host-root", default=str(DEFAULT_REPOS_HOST_ROOT))
    parser.add_argument("--projects", nargs="+", default=DEFAULT_PROJECTS)
    parser.add_argument("--cases-per-project", type=int, default=DEFAULT_CASES_PER_PROJECT)
    parser.add_argument("--min-projects", type=int, default=3)
    parser.add_argument("--output", default="docs/benchmarks/context-quality-live-projects.report.json")
    parser.add_argument("--markdown", default="docs/benchmarks/context-quality-live-projects.report.md")
    parser.add_argument("--run-date", default=datetime.now(timezone.utc).date().isoformat())
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = asyncio.run(build_report(args))

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.markdown:
        markdown_path = Path(args.markdown)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_markdown(report), encoding="utf-8")

    summary = report["summary"]
    print("\n=== ContextGraph Live Project HPS Benchmark ===\n")
    print(f"Projects: {len(report['liveBenchmark']['projects'])}")
    print(f"Cases: {summary['caseCount']}")
    print(f"Average baseline HPS: {summary['avgBaselineHps']}")
    print(f"Average CG HPS: {summary['avgCgHps']}")
    print(f"Average HPS reduction: {summary['avgHpsReductionPercent']}%")
    print(f"Average token reduction: {summary['avgTokenReductionPercent']}%")
    print(f"Report saved to: {output_path}")
    if args.markdown:
        print(f"Markdown saved to: {args.markdown}")


if __name__ == "__main__":
    main()