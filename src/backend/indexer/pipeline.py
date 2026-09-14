"""Publish complete repository generations without exposing partial graph writes.

Incremental jobs reuse versioned parse evidence for unchanged files. Nodes are
built before relationships, including incoming edges from unchanged callers.
"""

from __future__ import annotations

import ast
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import structlog

from backend import runtime_config
from backend.graph.client import GraphClient
from backend.graph import schema as S
from backend.indexer.call_analyzer import CallAnalyzer, RawCall
from backend.indexer.hasher import hash_variable_flows, sha256_file
from backend.indexer.language_catalog import (
    is_supported_parser_file,
    parser_language_ids_for_path,
)
from backend.indexer.parser import ParsedFile, SourceParser, discover_files, path_to_module
from backend.indexer.snapshot import (
    INDEX_FORMAT_VERSION, FileSnapshot, SymbolResolver, call_key, parse_snapshot,
)
from backend.indexer.paths import (
    RepositoryPathError,
    normalize_repo_path as _normalize_repo_path,
    resolve_repo_root as _resolve_repo_root,
    resolve_changed_path as _resolve_changed_path,
)

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
        if not candidate.is_relative_to(_resolve_repo_root(repo_path)):
            raise ValueError("Import path is outside the registered repository")

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
        return self._index_generation(repo_path, None)

    def index_incremental(self, repo_path: str, changed_paths: list[str]) -> dict:
        return self._index_generation(repo_path, changed_paths)

    def _index_generation(self, repo_path: str, changed_paths: list[str] | None) -> dict:
        root = _resolve_repo_root(repo_path).resolve()
        disabled = runtime_config.get_disabled_parser_languages()
        full = changed_paths is None
        requested = sorted({
            _resolve_changed_path(repo_path, root, path)
            for path in (
                discover_files(str(root), disabled_languages=disabled)
                if full else changed_paths
            )
        })
        requested = [path for path in requested if is_supported_parser_file(path)]
        stats = {"files": 0, "skipped": 0, "symbols": 0, "calls": 0, "imports": 0,
                 "variables": 0, "variable_flows": 0, "errors": 0}
        with self._graph.atomic_update() as stage:
            worker = IndexPipeline(stage)
            old_paths: set[str] = set()
            cached: dict[str, FileSnapshot] = {}
            legacy: set[str] = set()
            for path, content_hash, metadata, version in stage.query(S.QUERY_FILE_SNAPSHOTS).result_set:
                try:
                    normalized = _resolve_changed_path(
                        str(root), root, _normalize_repo_path(path)
                    )
                except RepositoryPathError:
                    log.warning("pipeline.foreign_metadata_ignored", repo_path=str(root))
                    continue
                old_paths.add(path)
                if not full and version == INDEX_FORMAT_VERSION and metadata:
                    snapshot = FileSnapshot.deserialize(metadata)
                    if snapshot.parsed.path != normalized or snapshot.content_hash != content_hash:
                        raise ValueError(f"Inconsistent index metadata for {path}; run a full rebuild")
                    cached[normalized] = snapshot
                else:
                    legacy.add(normalized)
            if full and not requested and old_paths:
                raise ValueError("Empty repository scan would erase the existing graph; use explicit incremental deletions")

            snapshots = {} if full else dict(cached)
            pending = set(requested) | (legacy if not full else set())
            mutated = full or bool(legacy)
            to_parse: list[str] = []
            for path in sorted(pending):
                languages = parser_language_ids_for_path(path)
                if (languages and languages.issubset(disabled)) or (
                    path in cached and cached[path].parsed.language in disabled
                ):
                    mutated = mutated or path in snapshots or path in legacy
                    snapshots.pop(path, None)
                    stats["skipped"] += 1
                elif not Path(path).is_file():
                    if path not in requested:
                        raise FileNotFoundError(f"Previously indexed source is unavailable: {path}")
                    mutated = mutated or path in snapshots or path in legacy
                    snapshots.pop(path, None)
                elif not full and path in cached and sha256_file(path) == cached[path].content_hash:
                    stats["skipped"] += 1
                else:
                    to_parse.append(path)
            with ThreadPoolExecutor(max_workers=_adaptive_workers(len(to_parse))) as executor:
                for snapshot in executor.map(parse_snapshot, to_parse):
                    path = snapshot.parsed.path
                    mutated = True
                    if snapshot.parsed.language in disabled:
                        snapshots.pop(path, None)
                        stats["skipped"] += 1
                    else:
                        snapshots[path] = snapshot
                        stats["files"] += 1
            if not mutated:
                stage.discard_update()
                stats["symbols"] = worker._count_symbols()
                return stats
            ordered = [snapshots[path] for path in sorted(snapshots)]
            resolver = SymbolResolver(ordered, root)

            for path in sorted(old_paths):
                worker._delete_file_subgraph(path)
            for alias in sorted({repo_path, str(root)}):
                stage.query(S.DELETE_REPO, {"repo_path": alias})
            worker._upsert_repo(str(root))
            for snapshot in ordered:
                worker._write_snapshot_nodes(str(root), snapshot)
            for snapshot in ordered:
                parsed = snapshot.parsed
                targets = resolver.call_map(snapshot)
                stats["calls"] += worker._write_call_edges_batch(parsed.calls, targets)
                imports = resolver.imports(snapshot)
                if imports:
                    stage.query(S.BATCH_EDGE_FILE_IMPORTS, {
                        "rows": [{"src_path": parsed.path, "target_path": target} for target in imports]
                    })
                stats["imports"] += len(imports)
                stats["variable_flows"] += worker._write_cross_scope_variable_flows(parsed.calls, targets)
                stats["variables"] += len(parsed.variables)
                stats["variable_flows"] += len(parsed.variable_flows)
            for snapshot in ordered:
                if snapshot.parsed.path in to_parse and sha256_file(snapshot.parsed.path) != snapshot.content_hash:
                    raise RuntimeError(f"Source changed before index publication: {snapshot.parsed.path}")
                stage.query(S.SET_FILE_SNAPSHOT, {
                    "path": snapshot.parsed.path,
                    "metadata": snapshot.serialize(),
                    "version": INDEX_FORMAT_VERSION,
                })
            stats["symbols"] = worker._count_symbols()
        log.info("pipeline.generation.published", repo_path=str(root), full=full, **stats)
        return stats

    def _write_snapshot_nodes(self, repo_path: str, snapshot: FileSnapshot) -> None:
        from backend.indexer.hasher import hash_symbols, hash_calls, hash_imports

        parsed = snapshot.parsed
        self._graph.query(S.MERGE_FILE, {
            "path": parsed.path, "language": parsed.language,
            "content_hash": snapshot.content_hash,
            "symbols_hash": hash_symbols(parsed), "calls_hash": hash_calls(parsed),
            "imports_hash": hash_imports(parsed), "variables_hash": hash_variable_flows(parsed),
        })
        self._graph.query(S.EDGE_REPO_CONTAINS_FILE, {"repo_path": repo_path, "file_path": parsed.path})
        if parsed.symbols:
            self._graph.query(S.BATCH_MERGE_SYMBOLS, {"rows": [
                {"qualified_name": symbol.qualified_name, "name": symbol.name,
                 "symbol_type": symbol.symbol_type, "file_path": symbol.file_path,
                 "line_start": symbol.line_start, "line_end": symbol.line_end}
                for symbol in parsed.symbols
            ]})
            self._graph.query(S.BATCH_EDGE_FILE_DEFINES_SYMBOL, {"rows": [
                {"file_path": parsed.path, "qualified_name": symbol.qualified_name}
                for symbol in parsed.symbols
            ]})
        self._write_variable_flow_edges(parsed)

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
        return self.index_incremental(repo_path, [file_path])

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
            if (callee_qname := symbol_map.get(call_key(rc)) or symbol_map.get(rc.callee_name))
        ]
        if rows:
            with self._graph_lock:
                self._graph.query(S.BATCH_EDGE_SYMBOL_CALLS, {"rows": rows})
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
            with self._graph_lock:
                self._graph.query(S.BATCH_EDGE_FILE_IMPORTS, {"rows": rows})
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
            positions: dict[str, int] = {}
            for row in var_rows:
                scope = row["scope_qname"]
                row["parameter_index"] = positions.get(scope, 0)
                if row["role"] == "parameter":
                    positions[scope] = positions.get(scope, 0) + 1
            with self._graph_lock:
                self._graph.query(S.BATCH_MERGE_VARIABLES, {"rows": var_rows})
                self._graph.query(S.BATCH_EDGE_SYMBOL_HAS_VARIABLE, {"rows": has_var_rows})
            stats["variables"] = len(var_rows)

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
            with self._graph_lock:
                self._graph.query(S.BATCH_EDGE_VARIABLE_FLOWS, {"rows": flow_rows})
            stats["variable_flows"] = len(flow_rows)
        return stats

    def _write_cross_scope_variable_flows(self, raw_calls: list[RawCall], symbol_map: dict[str, str]) -> int:
        # Resolve callees and collect unique ones for batch parameter lookup
        resolved: list[tuple[RawCall, str]] = [
            (call, callee_qname)
            for call in raw_calls
            if (callee_qname := symbol_map.get(call_key(call)) or symbol_map.get(call.callee_name))
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
            with self._graph_lock:
                self._graph.query(S.BATCH_EDGE_VARIABLE_FLOWS, {"rows": flow_rows})
        return len(flow_rows)

    def _get_scope_parameter_qnames(self, scope_qname: str) -> list[str]:
        cache = self._get_scope_parameters_batch([scope_qname])
        return cache.get(scope_qname, [])

    def _get_scope_parameters_batch(self, scope_qnames: list[str]) -> dict[str, list[str]]:
        """Batch-load parameter qnames for multiple scopes in a single query."""
        if not scope_qnames:
            return {}
        result = self._graph.query(
            S.BATCH_QUERY_SCOPE_PARAMETERS,
            {"scope_qnames": scope_qnames},
        )
        cache: dict[str, list[str]] = {}
        for row in result.result_set:
            if row and len(row) >= 2 and row[0] and row[1]:
                cache.setdefault(row[0], []).append(row[1])
        return cache

    def _get_stored_hash(self, file_path: str) -> str | None:
        with self._graph_lock:
            result = self._graph.query(S.QUERY_FILE_HASH, {"path": file_path})
        rows = result.result_set
        if rows and rows[0][0]:
            return rows[0][0]
        return None

    def _load_symbol_map(self) -> dict[str, str]:
        result = self._graph.query("MATCH (s:Symbol) RETURN s.name, s.qualified_name")
        names: dict[str, list[str]] = {}
        for name, qualified in result.result_set:
            if name:
                names.setdefault(name, []).append(qualified)
        return {name: candidates[0] for name, candidates in names.items() if len(candidates) == 1}

    def _count_symbols(self) -> int:
        result = self._graph.query(S.QUERY_COUNT_SYMBOLS)
        rows = result.result_set
        return rows[0][0] if rows else 0

    @staticmethod
    def _accumulate(total: dict, partial: dict) -> None:
        for k, v in partial.items():
            total[k] = total.get(k, 0) + v
