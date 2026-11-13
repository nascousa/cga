"""File-content hash tracking for incremental indexing deduplication.

On each incremental run the pipeline compares the SHA-256 digest of a file
against the last-indexed digest stored on the File node in FalkorDB.
If the digest is unchanged the file is skipped entirely, saving graph writes
and keeping indexing throughput high.

Symbol-level hashing: tracks separate digests for symbols, calls, and variable flows,
enabling fine-grained incremental updates when function signatures change
but the file mostly stays the same.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from backend.indexer.parser import ParsedFile


def sha256_file(file_path: str) -> str:
    """Return the hex SHA-256 digest of *file_path* contents."""
    h = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def file_changed(current_hash: str, stored_hash: str | None) -> bool:
    """Return True if the file must be re-indexed."""
    return stored_hash is None or current_hash != stored_hash


def hash_symbols(parsed: ParsedFile) -> str:
    """Hash the symbols defined in a parsed file (order-independent).
    
    This enables detection of symbol signature changes even if the file content varies
    in comments or formatting.
    """
    h = hashlib.sha256()
    for sym in sorted(
        parsed.symbols,
        key=lambda s: (s.qualified_name, s.symbol_type)
    ):
        line = f"{sym.qualified_name}:{sym.symbol_type}:{sym.line_start}:{sym.line_end}\n"
        h.update(line.encode("utf-8"))
        if sym.parameters is not None:
            h.update(json.dumps({
                "parameters": [asdict(parameter) for parameter in sym.parameters],
                "method_binding": sym.method_binding,
            }, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return h.hexdigest()


def hash_calls(parsed: ParsedFile) -> str:
    """Hash the calls extracted in a parsed file (for call graph changes)."""
    h = hashlib.sha256()
    evidence = [
        (
            call.caller_qname, call.callee_name,
            json.dumps(asdict(call), sort_keys=True, separators=(",", ":"))
            if call.arguments is not None else f"{call.caller_qname}->{call.callee_name}",
        )
        for call in parsed.calls
    ]
    for _, _, line in sorted(evidence):
        h.update(f"{line}\n".encode("utf-8"))
    return h.hexdigest()


def hash_imports(parsed: ParsedFile) -> str:
    """Hash the imports in a parsed file (for dependency tracking changes)."""
    h = hashlib.sha256()
    for imp in sorted(set(imp.imported_module for imp in parsed.imports)):
        h.update(f"{imp}\n".encode("utf-8"))
    return h.hexdigest()


def hash_variable_flows(parsed: ParsedFile) -> str:
    """Hash variable nodes and flows for data-flow tracking changes."""
    h = hashlib.sha256()
    for variable in sorted(parsed.variables, key=lambda item: item.qualified_name):
        h.update(
            f"var:{variable.qualified_name}:{variable.role}:{variable.line_number}\n".encode("utf-8")
        )
    for flow in sorted(
        parsed.variable_flows,
        key=lambda item: (item.scope_qname, item.source_qname, item.target_qname, item.flow_type, item.line_number),
    ):
        h.update(
            f"flow:{flow.scope_qname}:{flow.source_qname}->{flow.target_qname}:{flow.flow_type}:{flow.line_number}\n".encode("utf-8")
        )
    return h.hexdigest()
