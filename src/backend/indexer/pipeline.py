"""Indexing pipeline – orchestrates parsing and graph writes.

Full index   : scan the entire repo, upsert all entities.
Incremental  : re-index only the changed file paths, skipping hash-unchanged files.

Python supports CALLS edge extraction.
TS/JS support currently indexes files, symbols, and imports for repository-wide lookup.

Import tracking: resolve local imports (./utils, ../core) to actual file paths and write IMPORTS edges.

All graph writes use MERGE so re-indexing is safe and idempotent.
"""

from __future__ import annotations

import ast
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import structlog

from backend import runtime_config
from backend.graph.client import GraphClient
from backend.graph import schema as S
from backend.indexer.call_analyzer import CallAnalyzer, RawCall
from backend.indexer.hasher import file_changed, hash_variable_flows, sha256_file
from backend.indexer.language_catalog import (
    is_supported_parser_file,
    parser_language_ids_for_path,
)
from backend.indexer.parser import ParsedFile, SourceParser, discover_files, path_to_module

# Worker count bounds
_MIN_WORKERS = 2
_MAX_WORKERS = int(os.getenv("CG_INDEX_MAX_WORKERS", str(min(32, (os.cpu_count() or 4) * 2))))


def _adaptive_workers(file_count: int) -> int:
    """Return a worker count that scales with file_count.

    Scaling formula: workers = clamp(ceil(log2(file_count + 1)) * 2, MIN, MAX)

    file_count |  workers
    -----------+---------
            1  |   2
            4  |   4
           16  |   8
           64  |  12
          256  |  16
         1024  |  20
        >=big  |  MAX
    """
    if file_count <= 0:
        return _MIN_WORKERS
    raw = math.ceil(math.log2(file_count + 1)) * 2
    return max(_MIN_WORKERS, min(_MAX_WORKERS, raw))

log = structlog.get_logger()


def _normalize_repo_path(repo_path: str) -> str:
    """Return an indexable repo path in both host and container runtimes.

    In Docker dev, Windows-style paths (e.g. D:/Repos/OSAgent) are not directly
    visible. We map them to /repos/<name> when that path exists.
    """
    candidate = Path(repo_path)
    if candidate.exists():
        return str(candidate)

    normalized = repo_path.replace("\\", "/")
    marker = "/Repos/"
    idx = normalized.find(marker)
    if idx >= 0:
        tail = normalized[idx + len(marker):].strip("/")
        mapped = Path("/repos") / tail
        if mapped.exists():
            return str(mapped)

    return repo_path


def _resolve_repo_root(repo_path: str) -> Path:
    resolved = Path(_normalize_repo_path(repo_path))
    if not resolved.is_dir():
        raise FileNotFoundError(
            f"Repository path is not visible to the CGA indexer: {repo_path}. "
            "Mount the repository into the API/indexer runtime or trigger indexing through a local cga-relay."
        )
    return resolved


def _looks_like_windows_absolute_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return len(normalized) >= 3 and normalized[1:3] == ":/" and normalized[0].isalpha()


def _resolve_changed_path(repo_path: str, resolved_repo_path: Path, changed_path: str) -> str:
    candidate = Path(changed_path)
    if candidate.exists():
        return str(candidate)

    normalized = changed_path.replace("\\", "/")
    original_repo = repo_path.replace("\\", "/").rstrip("/")
    resolved_repo = resolved_repo_path.as_posix().rstrip("/")

    relative_path: str | None = None
    if original_repo and normalized.startswith(f"{original_repo}/"):
        relative_path = normalized[len(original_repo) + 1:]
    elif resolved_repo and normalized.startswith(f"{resolved_repo}/"):
        return normalized
    elif not Path(normalized).is_absolute() and not _looks_like_windows_absolute_path(normalized):
        relative_path = normalized

    if relative_path is not None:
        return str(resolved_repo_path / Path(*relative_path.split("/")))

    return str(candidate)


def _resolve_import_path(source_file: str, imported_module: str, repo_path: str) -> str | None:
    """Resolve relative import to actual file path (best effort).

    Handles:
    - Relative paths like "./utils", "../core"
    - Directory imports like "./handlers/auth"
    - Language-specific extensions

    Returns the resolved file path if found, None otherwise (e.g., external packages).
    """
    source_path = Path(source_file)
    source_dir = source_path.parent

    if imported_module.startswith("."):
        if imported_module.startswith("./"):
            rel = imported_module[2:]
        elif imported_module.startswith("../"):
            rel = imported_module
        else:
            rel = imported_module[1:]

        candidate = (source_dir / rel).resolve()

        if candidate.is_file():
            return str(candidate)

        for ext in [".py", ".ts", ".tsx", ".js", ".jsx"]:
            file_candidate = Path(str(candidate) + ext)
            if file_candidate.is_file():
                return str(file_candidate)

        if candidate.is_dir():
            for ext in [".py", ".ts", ".tsx", ".js", ".jsx"]:
                init_file = candidate / f"__init__{ext}" if ext == ".py" else candidate / f"index{ext}"
                if init_file.is_file():
                    return str(init_file)

    return None


class IndexPipeline:
    def __init__(self, graph: GraphClient) -> None:
        self._graph = graph
        self._parser = SourceParser()
        self._call_analyzer = CallAnalyzer()
        self._graph_lock = threading.Lock()

    def index_full(self, repo_path: str) -> dict:
        resolved_repo_path = _resolve_repo_root(repo_path)
        disabled_languages = runtime_config.get_disabled_parser_languages()
        files = list(
            discover_files(
                str(resolved_repo_path),
                disabled_languages=disabled_languages,
            )
        )
        workers = _adaptive_workers(len(files))
        log.info("pipeline.full.start", repo_path=repo_path, resolved_repo_path=str(resolved_repo_path), files=len(files), workers=workers)
        self._delete_repo_subgraph(repo_path)
        self._upsert_repo(repo_path)
        stats: dict = {"files": 0, "skipped": 0, "symbols": 0, "calls": 0, "imports": 0, "variables": 0, "variable_flows": 0, "errors": 0}
        symbol_map: dict[str, str] = {}
        symbol_map_lock = threading.Lock()

        def _process_full(fpath: str) -> dict:
            return self._index_file(
                repo_path,
                fpath,
                symbol_map,
                force=True,
                symbol_map_lock=symbol_map_lock,
                disabled_languages=disabled_languages,
            )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_process_full, fp): fp for fp in files}
            for future in as_completed(futures):
                try:
                    self._accumulate(stats, future.result())
                except Exception as exc:
                    log.error("pipeline.full.worker_error", path=futures[future], error=str(exc))
                    stats["errors"] = stats.get("errors", 0) + 1

        stats["symbols"] = self._count_symbols()
        log.info("pipeline.full.done", **stats)
        return stats

    def index_incremental(self, repo_path: str, changed_paths: list[str]) -> dict:
        resolved_repo_path = _resolve_repo_root(repo_path)
        disabled_languages = runtime_config.get_disabled_parser_languages()
        log.info("pipeline.incremental.start", changed=len(changed_paths), repo_path=repo_path, resolved_repo_path=str(resolved_repo_path))
        self._upsert_repo(repo_path)
        stats: dict = {"files": 0, "skipped": 0, "symbols": 0, "calls": 0, "imports": 0, "variables": 0, "variable_flows": 0, "errors": 0}
        symbol_map = self._load_symbol_map()

        valid_paths: list[str] = []
        for fpath in changed_paths:
            normalized_fpath = _resolve_changed_path(repo_path, resolved_repo_path, fpath)
            if not is_supported_parser_file(normalized_fpath):
                continue
            valid_paths.append(normalized_fpath)

        workers = _adaptive_workers(len(valid_paths))
        symbol_map_lock = threading.Lock()
        log.info("pipeline.incremental.workers", file_count=len(valid_paths), workers=workers)

        def _process(fpath: str) -> dict:
            result = self._index_file(
                repo_path,
                fpath,
                symbol_map,
                force=False,
                symbol_map_lock=symbol_map_lock,
                disabled_languages=disabled_languages,
            )
            return result

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_process, fp): fp for fp in valid_paths}
            for future in as_completed(futures):
                try:
                    self._accumulate(stats, future.result())
                except Exception as exc:
                    log.error("pipeline.incremental.worker_error", path=futures[future], error=str(exc))
                    stats["errors"] = stats.get("errors", 0) + 1

        stats["symbols"] = self._count_symbols()
        log.info("pipeline.incremental.done", **stats)
        return stats

    def _upsert_repo(self, repo_path: str) -> None:
        name = Path(repo_path).name
        self._graph.query(S.MERGE_REPO, {"path": repo_path, "name": name})

    def _delete_repo_subgraph(self, repo_path: str) -> None:
        if not self._repo_exists(repo_path):
            return
        result = self._locked_query(S.QUERY_REPO_FILE_PATHS, {"repo_path": repo_path})
        for row in result.result_set:
            if row and row[0]:
                self._delete_file_subgraph(row[0])
        self._locked_query(S.DELETE_REPO, {"repo_path": repo_path})

    def _delete_file_subgraph(self, file_path: str) -> None:
        self._locked_query(S.DELETE_FILE_VARIABLES, {"file_path": file_path})
        self._locked_query(S.DELETE_FILE_SYMBOLS, {"file_path": file_path})
        self._locked_query(S.DELETE_FILE, {"file_path": file_path})

    def _repo_exists(self, repo_path: str) -> bool:
        result = self._locked_query(S.QUERY_REPO_EXISTS, {"repo_path": repo_path})
        rows = result.result_set
        return bool(rows and rows[0] and rows[0][0])

    def _index_file(
        self,
        repo_path: str,
        file_path: str,
        symbol_map: dict[str, str],
        force: bool,
        symbol_map_lock: threading.Lock | None = None,
        disabled_languages: frozenset[str] | None = None,
    ) -> dict:
        result = {"files": 0, "skipped": 0, "symbols": 0, "calls": 0, "imports": 0, "variables": 0, "variable_flows": 0, "errors": 0}
        try:
            from backend.indexer.hasher import hash_symbols, hash_calls, hash_imports

            if not Path(file_path).exists():
                self._delete_file_subgraph(file_path)
                return result

            disabled = disabled_languages or frozenset()
            candidate_languages = parser_language_ids_for_path(file_path)
            if candidate_languages and candidate_languages.issubset(disabled):
                self._delete_file_subgraph(file_path)
                result["skipped"] = 1
                return result

            current_hash = sha256_file(file_path)
            requires_language_gate = bool(candidate_languages & disabled)
            if not force and not requires_language_gate:
                stored = self._get_stored_hash(file_path)
                if not file_changed(current_hash, stored):
                    result["skipped"] = 1
                    return result

            parsed: ParsedFile = self._parser.parse(file_path)
            if parsed.language in disabled:
                self._delete_file_subgraph(file_path)
                result["skipped"] = 1
                return result
            if not force and requires_language_gate:
                stored = self._get_stored_hash(file_path)
                if not file_changed(current_hash, stored):
                    result["skipped"] = 1
                    return result
            if parsed.parse_error:
                log.warning("pipeline.parse_error", path=file_path, error=parsed.parse_error)

            current_symbols_hash = hash_symbols(parsed)
            current_calls_hash = hash_calls(parsed)
            current_imports_hash = hash_imports(parsed)
            current_variables_hash = hash_variable_flows(parsed)

            # For changed files we rebuild the complete file-local subgraph after clearing
            # stale nodes/edges first. This preserves correctness for removed symbols,
            # imports, variables, and call/flow relationships.
            if not force:
                self._delete_file_subgraph(file_path)

            with self._graph_lock:
                self._graph.query(
                    S.MERGE_FILE,
                    {
                        "path": file_path,
                        "language": parsed.language,
                        "content_hash": current_hash,
                        "symbols_hash": current_symbols_hash,
                        "calls_hash": current_calls_hash,
                        "imports_hash": current_imports_hash,
                        "variables_hash": current_variables_hash,
                    },
                )
                self._graph.query(
                    S.EDGE_REPO_CONTAINS_FILE,
                    {"repo_path": repo_path, "file_path": file_path},
                )

            sym_rows = [
                {
                    "qualified_name": sym.qualified_name,
                    "name": sym.name,
                    "symbol_type": sym.symbol_type,
                    "file_path": sym.file_path,
                    "line_start": sym.line_start,
                    "line_end": sym.line_end,
                }
                for sym in parsed.symbols
            ]
            def_rows = [
                {"file_path": file_path, "qualified_name": sym.qualified_name}
                for sym in parsed.symbols
            ]
            if sym_rows:
                with self._graph_lock:
                    self._graph.query(S.BATCH_MERGE_SYMBOLS, {"rows": sym_rows})
                    self._graph.query(S.BATCH_EDGE_FILE_DEFINES_SYMBOL, {"rows": def_rows})
            new_symbols = {sym.name: sym.qualified_name for sym in parsed.symbols}
            if symbol_map_lock:
                with symbol_map_lock:
                    symbol_map.update(new_symbols)
            else:
                symbol_map.update(new_symbols)
            result["symbols"] += len(sym_rows)

            raw_calls = (
                self._extract_python_raw_calls(file_path)
                if parsed.language == "python"
                else parsed.calls
            )
            result["calls"] = self._write_call_edges_batch(raw_calls, symbol_map)

            result["imports"] = self._write_import_edges(file_path, parsed, repo_path)

            variable_stats = self._write_variable_flow_edges(parsed)
            result["variables"] = variable_stats["variables"]
            result["variable_flows"] = variable_stats["variable_flows"]

            result["variable_flows"] += self._write_cross_scope_variable_flows(raw_calls, symbol_map)

            result["files"] = 1
        except Exception as exc:
            log.error("pipeline.file_error", path=file_path, error=str(exc))
            result["errors"] = 1
        return result

    def _locked_query(self, cypher: str, params: dict | None = None):
        """Execute a graph query under the pipeline-level lock."""
        with self._graph_lock:
            return self._graph.query(cypher, params)

    def _extract_python_raw_calls(self, file_path: str) -> list[RawCall]:
        try:
            source = Path(file_path).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=file_path)
        except (SyntaxError, OSError):
            return []

        module_qname = path_to_module(file_path)
        return self._call_analyzer.extract(tree, file_path, module_qname)

    def _write_call_edges_batch(self, raw_calls: list[RawCall], symbol_map: dict[str, str]) -> int:
        rows = [
            {"caller_qname": rc.caller_qname, "callee_qname": callee_qname}
            for rc in raw_calls
            if (callee_qname := symbol_map.get(rc.callee_name)) and callee_qname != rc.caller_qname
        ]
        if rows:
            try:
                with self._graph_lock:
                    self._graph.query(S.BATCH_EDGE_SYMBOL_CALLS, {"rows": rows})
            except Exception:
                pass
        return len(rows)

    def _write_python_call_edges(self, raw_calls: list[RawCall], symbol_map: dict[str, str]) -> int:
        return self._write_call_edges_batch(raw_calls, symbol_map)

    def _write_ts_js_call_edges(self, raw_calls: list[RawCall], symbol_map: dict[str, str]) -> int:
        return self._write_call_edges_batch(raw_calls, symbol_map)

    def _write_import_edges(self, file_path: str, parsed: ParsedFile, repo_path: str) -> int:
        """Resolve and write IMPORTS edges for local imports."""
        rows = [
            {"src_path": file_path, "target_path": target_path}
            for imp in parsed.imports
            if (target_path := _resolve_import_path(file_path, imp.imported_module, repo_path))
        ]
        if rows:
            try:
                with self._graph_lock:
                    self._graph.query(S.BATCH_EDGE_FILE_IMPORTS, {"rows": rows})
            except Exception:
                pass
        return len(rows)

    def _write_variable_flow_edges(self, parsed: ParsedFile) -> dict[str, int]:
        stats = {"variables": 0, "variable_flows": 0}
        var_rows = [
            {
                "qualified_name": v.qualified_name,
                "name": v.name,
                "scope_qname": v.scope_qname,
                "file_path": v.file_path,
                "line_number": v.line_number,
                "role": v.role,
            }
            for v in parsed.variables
        ]
        has_var_rows = [
            {"scope_qname": v.scope_qname, "variable_qname": v.qualified_name}
            for v in parsed.variables
        ]
        if var_rows:
            try:
                with self._graph_lock:
                    self._graph.query(S.BATCH_MERGE_VARIABLES, {"rows": var_rows})
                    self._graph.query(S.BATCH_EDGE_SYMBOL_HAS_VARIABLE, {"rows": has_var_rows})
                stats["variables"] = len(var_rows)
            except Exception:
                pass

        flow_rows = [
            {
                "source_qname": flow.source_qname,
                "target_qname": flow.target_qname,
                "scope_qname": flow.scope_qname,
                "line_number": flow.line_number,
                "flow_type": flow.flow_type,
            }
            for flow in parsed.variable_flows
        ]
        if flow_rows:
            try:
                with self._graph_lock:
                    self._graph.query(S.BATCH_EDGE_VARIABLE_FLOWS, {"rows": flow_rows})
                stats["variable_flows"] = len(flow_rows)
            except Exception:
                pass
        return stats

    def _write_cross_scope_variable_flows(self, raw_calls: list[RawCall], symbol_map: dict[str, str]) -> int:
        # Resolve callees and collect unique ones for batch parameter lookup
        resolved: list[tuple[RawCall, str]] = [
            (call, callee_qname)
            for call in raw_calls
            if (callee_qname := symbol_map.get(call.callee_name))
        ]
        if not resolved:
            return 0

        unique_callees = list({cq for _, cq in resolved})
        parameter_cache = self._get_scope_parameters_batch(unique_callees)

        flow_rows: list[dict] = []
        for call, callee_qname in resolved:
            callee_params = parameter_cache.get(callee_qname, [])
            for index, arg_name in enumerate((call.arg_names or [])[: len(callee_params)]):
                flow_rows.append({
                    "source_qname": f"{call.caller_qname}:{arg_name}",
                    "target_qname": callee_params[index],
                    "scope_qname": call.caller_qname,
                    "line_number": 0,
                    "flow_type": "argument",
                })
            if call.result_var_name:
                flow_rows.append({
                    "source_qname": f"{callee_qname}:__return__",
                    "target_qname": f"{call.caller_qname}:{call.result_var_name}",
                    "scope_qname": call.caller_qname,
                    "line_number": 0,
                    "flow_type": "call_return",
                })
        if flow_rows:
            try:
                with self._graph_lock:
                    self._graph.query(S.BATCH_EDGE_VARIABLE_FLOWS, {"rows": flow_rows})
            except Exception:
                pass
        return len(flow_rows)

    def _get_scope_parameter_qnames(self, scope_qname: str) -> list[str]:
        cache = self._get_scope_parameters_batch([scope_qname])
        return cache.get(scope_qname, [])

    def _get_scope_parameters_batch(self, scope_qnames: list[str]) -> dict[str, list[str]]:
        """Batch-load parameter qnames for multiple scopes in a single query."""
        if not scope_qnames:
            return {}
        try:
            result = self._graph.query(
                S.BATCH_QUERY_SCOPE_PARAMETERS,
                {"scope_qnames": scope_qnames},
            )
            cache: dict[str, list[str]] = {}
            for row in result.result_set:
                if row and len(row) >= 2 and row[0] and row[1]:
                    cache.setdefault(row[0], []).append(row[1])
            return cache
        except Exception:
            return {}

    def _get_stored_hash(self, file_path: str) -> str | None:
        with self._graph_lock:
            result = self._graph.query(S.QUERY_FILE_HASH, {"path": file_path})
        rows = result.result_set
        if rows and rows[0][0]:
            return rows[0][0]
        return None

    def _load_symbol_map(self) -> dict[str, str]:
        try:
            result = self._graph.query("MATCH (s:Symbol) RETURN s.name, s.qualified_name LIMIT 100000")
            return {row[0]: row[1] for row in result.result_set if row[0]}
        except Exception:
            return {}

    def _count_symbols(self) -> int:
        result = self._graph.query(S.QUERY_COUNT_SYMBOLS)
        rows = result.result_set
        return rows[0][0] if rows else 0

    @staticmethod
    def _accumulate(total: dict, partial: dict) -> None:
        for k, v in partial.items():
            total[k] = total.get(k, 0) + v
