"""Durable parse evidence and deterministic, scope-aware relationship resolution."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path

from backend.indexer.call_analyzer import CallAnalyzer
from backend.indexer.hasher import sha256_file
from backend.indexer.parser import (
    ParsedFile, ParsedImport, ParsedSymbol, ParsedVariable, ParsedVariableFlow,
    RawCall, SourceParser, path_to_module,
)


INDEX_FORMAT_VERSION = 2


@dataclass
class FileSnapshot:
    parsed: ParsedFile
    content_hash: str
    bindings: dict[str, dict[str, str]] = field(default_factory=dict)

    def serialize(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def deserialize(cls, value: str) -> FileSnapshot:
        data = json.loads(value)
        parsed = data["parsed"]
        return cls(
            ParsedFile(
                path=parsed["path"],
                language=parsed["language"],
                symbols=[ParsedSymbol(**row) for row in parsed["symbols"]],
                imports=[ParsedImport(**row) for row in parsed["imports"]],
                calls=[RawCall(**row) for row in parsed["calls"]],
                variables=[ParsedVariable(**row) for row in parsed["variables"]],
                variable_flows=[ParsedVariableFlow(**row) for row in parsed["variable_flows"]],
                parse_error=parsed.get("parse_error"),
            ),
            data["content_hash"],
            data.get("bindings", {}),
        )


def parse_snapshot(path: str, parser: SourceParser | None = None) -> FileSnapshot:
    source_path = Path(path)
    before_stat = source_path.stat()
    before = sha256_file(path)
    parsed = (parser or SourceParser()).parse(path)
    if parsed.parse_error:
        raise ValueError(f"Cannot parse {path}: {parsed.parse_error}")
    bindings: dict[str, dict[str, str]] = {}
    if parsed.language == "python":
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)
        parsed.calls = CallAnalyzer().extract(tree, path, path_to_module(path))
        module = path_to_module(path)
        scopes = {symbol.line_start: symbol.qualified_name for symbol in parsed.symbols}

        class Imports(ast.NodeVisitor):
            scope = module

            def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                previous = self.scope
                self.scope = scopes.get(node.lineno, f"{previous}.{node.name}")
                for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                    bindings[f"{self.scope}:{argument.arg}"] = {"module": "", "name": ""}
                self.generic_visit(node)
                self.scope = previous

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Import(self, node: ast.Import) -> None:
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    target = alias.name if alias.asname else local
                    bindings[f"{self.scope}:{local}"] = {"module": target, "name": ""}

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                for alias in node.names:
                    if alias.name != "*":
                        bindings[f"{self.scope}:{alias.asname or alias.name}"] = {
                            "module": "." * node.level + (node.module or ""),
                            "name": alias.name,
                        }

        Imports().visit(tree)
    after_hash = sha256_file(path)
    after_stat = source_path.stat()
    changed_identity = any(
        getattr(before_stat, key) != getattr(after_stat, key)
        for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    )
    if after_hash != before or changed_identity:
        raise RuntimeError(f"Source changed while being parsed: {path}; retry indexing")
    return FileSnapshot(parsed, before, bindings)


def call_key(call: RawCall) -> str:
    return f"{call.caller_qname}:{call.callee_qualifier or call.callee_name}"


class SymbolResolver:
    def __init__(self, snapshots: list[FileSnapshot], root: Path) -> None:
        self.root = root
        self.files = {Path(s.parsed.path).resolve(): s for s in snapshots}
        self.symbols: dict[str, ParsedSymbol] = {}
        self.by_name: dict[str, list[ParsedSymbol]] = {}
        for snapshot in snapshots:
            for symbol in snapshot.parsed.symbols:
                existing = self.symbols.get(symbol.qualified_name)
                if existing is not None and existing.file_path != symbol.file_path:
                    raise ValueError(f"Ambiguous symbol identity: {symbol.qualified_name}")
                self.symbols[symbol.qualified_name] = symbol
                self.by_name.setdefault(symbol.name, []).append(symbol)

    def module_file(self, source: str, module: str, language: str) -> Path | None:
        if language == "python":
            level = len(module) - len(module.lstrip("."))
            relative = Path(*module.lstrip(".").split(".")) if module.lstrip(".") else Path()
            if level:
                base = Path(source).parent
                for _ in range(level - 1):
                    base = base.parent
                candidates = [base / relative]
            else:
                candidates = [self.root / relative, self.root / "src" / relative, Path(source).parent / relative]
            extensions = (".py",)
            initializers = ("__init__.py",)
        else:
            if not module.startswith("."):
                return None
            candidates = [Path(source).parent / module]
            extensions = (".ts", ".tsx", ".js", ".jsx", ".py")
            initializers = ("index.ts", "index.tsx", "index.js", "index.jsx", "__init__.py")
        for candidate in candidates:
            paths = [candidate, *(Path(str(candidate) + ext) for ext in extensions)]
            paths.extend(candidate / name for name in initializers)
            for path in paths:
                normalized = path.resolve()
                if normalized.is_relative_to(self.root) and normalized in self.files:
                    return normalized
        return None

    def imports(self, snapshot: FileSnapshot) -> list[str]:
        modules = {item.imported_module for item in snapshot.parsed.imports}
        modules.update(binding["module"] for binding in snapshot.bindings.values() if binding["module"])
        return sorted({
            str(target)
            for module in modules
            if (target := self.module_file(snapshot.parsed.path, module, snapshot.parsed.language))
        })

    def _bound_target(self, snapshot: FileSnapshot, binding: dict[str, str], suffix: list[str]) -> str | None:
        module = binding["module"]
        parts = ([binding["name"]] if binding["name"] else []) + suffix
        for count in range(len(parts), -1, -1):
            prefix = ".".join(parts[:count])
            module_name = module + ("" if not prefix or module.endswith(".") else ".") + prefix
            target = self.module_file(snapshot.parsed.path, module_name, "python")
            if target is None:
                continue
            qualified = ".".join([path_to_module(str(target)), *parts[count:]])
            if qualified in self.symbols:
                return qualified
        return None

    def resolve(self, snapshot: FileSnapshot, call: RawCall) -> str | None:
        qualifier = call.callee_qualifier or call.callee_name
        parts = qualifier.split(".")
        module = path_to_module(snapshot.parsed.path)
        scope = call.caller_qname
        if parts[0] in {"self", "cls"} and len(parts) == 2:
            target = f"{scope.rsplit('.', 1)[0]}.{parts[1]}"
            return target if target in self.symbols else None
        while scope.startswith(module):
            binding = snapshot.bindings.get(f"{scope}:{parts[0]}")
            if binding is not None:
                if not binding["module"] and not binding["name"]:
                    return None
                return self._bound_target(snapshot, binding, parts[1:])
            candidate = f"{scope}.{qualifier}"
            if candidate in self.symbols:
                return candidate
            if scope == module:
                break
            scope = scope.rsplit(".", 1)[0]
        local = [
            symbol for symbol in self.by_name.get(call.callee_name, [])
            if symbol.file_path == snapshot.parsed.path
        ]
        if len(local) == 1:
            return local[0].qualified_name
        imported = set(self.imports(snapshot))
        candidates = [
            symbol for symbol in self.by_name.get(call.callee_name, [])
            if symbol.file_path in imported
        ]
        if len(candidates) == 1:
            return candidates[0].qualified_name
        if snapshot.parsed.language != "python" and len(self.by_name.get(call.callee_name, [])) == 1:
            return self.by_name[call.callee_name][0].qualified_name
        return None

    def call_map(self, snapshot: FileSnapshot) -> dict[str, str]:
        return {
            call_key(call): target for call in snapshot.parsed.calls
            if (target := self.resolve(snapshot, call)) is not None
        }
