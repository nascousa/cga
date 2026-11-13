"""Durable parse evidence and deterministic, scope-aware relationship resolution."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path

from backend.indexer.call_analyzer import (
    CallAnalyzer, CallArgument, PythonParameter, PythonScope, python_parameters,
    python_scopes, walk_python_scope_context,
)
from backend.indexer.hasher import sha256_file
from backend.indexer.parser import (
    ParsedFile, ParsedImport, ParsedSymbol, ParsedVariable, ParsedVariableFlow,
    RawCall, SourceParser, path_to_module,
)


INDEX_FORMAT_VERSION = 4


@dataclass
class FileSnapshot:
    parsed: ParsedFile
    content_hash: str
    bindings: dict[str, dict[str, str]] = field(default_factory=dict)
    scope_parents: dict[str, str] = field(default_factory=dict)

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
                symbols=[
                    ParsedSymbol(**{
                        **row,
                        "parameters": (
                            [PythonParameter(**parameter) for parameter in row["parameters"]]
                            if row.get("parameters") is not None else None
                        ),
                    })
                    for row in parsed["symbols"]
                ],
                imports=[ParsedImport(**row) for row in parsed["imports"]],
                calls=[
                    RawCall(**{
                        **row,
                        "arguments": (
                            [CallArgument(**argument) for argument in row["arguments"]]
                            if row.get("arguments") is not None else None
                        ),
                    })
                    for row in parsed["calls"]
                ],
                variables=[ParsedVariable(**row) for row in parsed["variables"]],
                variable_flows=[ParsedVariableFlow(**row) for row in parsed["variable_flows"]],
                parse_error=parsed.get("parse_error"),
            ),
            data["content_hash"],
            data.get("bindings", {}),
            data.get("scope_parents", {}),
        )


def _python_bindings(scope: PythonScope, scopes: dict[ast.AST, PythonScope]) -> dict[str, dict[str, str]]:
    bindings: dict[str, dict[str, str]] = {}
    unknown = {"module": "", "name": ""}

    def bind(name: str, target: dict[str, str]) -> None:
        previous = bindings.get(name)
        bindings[name] = target if previous is None or previous == target else dict(unknown)

    if isinstance(scope.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        parameters = python_parameters(scope.node.args)
        for index, parameter in enumerate(parameters):
            target = dict(unknown)
            if index == 0 and scope.parent is not None and scope.method_binding in {"instance", "class"} and parameter.kind in {
                "positional_only", "positional_or_keyword",
            }:
                target.update(receiver=scope.parent.qualified_name, receiver_kind=scope.method_binding)
            bind(parameter.name, target)
    for name in scope.global_names | scope.nonlocal_names:
        bind(name, {**unknown, "scope": scope.variable_scope(name).qualified_name})
    for node, bound in walk_python_scope_context(
        scope.node, evaluate_annotations=scope.evaluate_annotations,
    ):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(node.name, {**unknown, "symbol": scopes[node].qualified_name})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                bind(local, {"module": alias.name if alias.asname else local, "name": ""})
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    bind(alias.asname or alias.name, {
                        "module": "." * node.level + (node.module or ""),
                        "name": alias.name,
                    })
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if node.id not in bound:
                bind(node.id, dict(unknown))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bind(node.name, dict(unknown))
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bind(node.name, dict(unknown))
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bind(node.rest, dict(unknown))
    return {f"{scope.qualified_name}:{name}": target for name, target in bindings.items()}


def parse_snapshot(path: str, parser: SourceParser | None = None) -> FileSnapshot:
    source_path = Path(path)
    before_stat = source_path.stat()
    before = sha256_file(path)
    parsed = (parser or SourceParser()).parse(path)
    if parsed.parse_error:
        raise ValueError(f"Cannot parse {path}: {parsed.parse_error}")
    bindings: dict[str, dict[str, str]] = {}
    scope_parents: dict[str, str] = {}
    if parsed.language == "python":
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)
        parsed.calls = CallAnalyzer().extract(tree, path, path_to_module(path))
        module = path_to_module(path)
        scopes = python_scopes(tree, module)
        by_node = {scope.node: scope for scope in scopes}
        for scope in scopes:
            bindings.update(_python_bindings(scope, by_node))
            if scope.lexical_parent is not None:
                scope_parents[scope.qualified_name] = scope.lexical_parent.qualified_name
    after_hash = sha256_file(path)
    after_stat = source_path.stat()
    changed_identity = any(
        getattr(before_stat, key) != getattr(after_stat, key)
        for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    )
    if after_hash != before or changed_identity:
        raise RuntimeError(f"Source changed while being parsed: {path}; retry indexing")
    return FileSnapshot(parsed, before, bindings, scope_parents)


def call_key(call: RawCall) -> str:
    key = f"{call.caller_qname}:{call.callee_qualifier or call.callee_name}"
    return key + "#comprehension-shadowed" if call.callee_is_shadowed else key


class SymbolResolver:
    def __init__(self, snapshots: list[FileSnapshot], root: Path) -> None:
        self.root = root
        self.files = {Path(s.parsed.path).resolve(): s for s in snapshots}
        self._module_files: dict[tuple[str, str, str], Path | None] = {}
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
        key = (source, module, language)
        if key not in self._module_files:
            self._module_files[key] = self._find_module_file(source, module, language)
        return self._module_files[key]

    def _find_module_file(self, source: str, module: str, language: str) -> Path | None:
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
        if call.callee_is_shadowed:
            return None
        qualifier = call.callee_qualifier or call.callee_name
        if snapshot.parsed.language == "python":
            target, receiver_kind = self._python_reference(snapshot, call.caller_qname, qualifier)
            return None if receiver_kind is not None else target
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

    def _python_reference(
        self, snapshot: FileSnapshot, caller: str, qualifier: str,
    ) -> tuple[str | None, str | None]:
        parts = qualifier.split(".")
        module = path_to_module(snapshot.parsed.path)
        scope = caller
        visited: set[str] = set()
        while scope not in visited and (scope == module or scope.startswith(module + ".")):
            visited.add(scope)
            binding = snapshot.bindings.get(f"{scope}:{parts[0]}")
            if binding is not None:
                if "scope" in binding:
                    scope = binding["scope"]
                    continue
                if "receiver" in binding or "symbol" in binding:
                    root = binding.get("receiver") or binding["symbol"]
                    target = ".".join([root, *parts[1:]])
                    receiver_kind = binding.get("receiver_kind") if len(parts) == 1 else None
                    return (target if target in self.symbols else None), receiver_kind
                if binding["module"]:
                    return self._bound_target(snapshot, binding, parts[1:]), None
                return None, None
            candidate = f"{scope}.{qualifier}"
            if candidate in self.symbols:
                return candidate, None
            if scope == module:
                break
            scope = snapshot.scope_parents.get(scope, scope.rsplit(".", 1)[0])
        return None, None

    def parameter_map(
        self, snapshot: FileSnapshot, targets: dict[str, str],
    ) -> dict[str, list[PythonParameter] | None]:
        """Return effective signatures, accounting for Python method descriptors."""
        if snapshot.parsed.language != "python":
            return {}
        signatures: dict[str, list[PythonParameter] | None] = {}
        for call in snapshot.parsed.calls:
            key = call_key(call)
            if key not in targets:
                continue
            symbol = self.symbols[targets[key]]
            parameters = symbol.parameters
            if parameters is not None and symbol.method_binding in {"instance", "class"}:
                qualifier = call.callee_qualifier or call.callee_name
                prefix, separator, _ = qualifier.rpartition(".")
                owner, receiver_kind = self._python_reference(snapshot, call.caller_qname, prefix) if separator else (None, None)
                if owner != symbol.qualified_name.rsplit(".", 1)[0]:
                    parameters = None
                elif symbol.method_binding == "class" or receiver_kind == "instance":
                    if parameters and parameters[0].kind in {"positional_only", "positional_or_keyword"}:
                        parameters = parameters[1:]
                    elif not parameters or parameters[0].kind != "var_positional":
                        parameters = None
            signatures[key] = parameters
        return signatures

    def call_map(self, snapshot: FileSnapshot) -> dict[str, str]:
        return {
            call_key(call): target for call in snapshot.parsed.calls
            if (target := self.resolve(snapshot, call)) is not None
        }
