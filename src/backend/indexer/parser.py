"""Source parser support for Python and basic TypeScript/JavaScript.

Extracts:
- Symbols: classes, functions, async functions, methods, interfaces, types, enums.
- Imports: Python import/from-import and JS/TS import/export/require statements.
- Calls: function/method calls (for call-graph construction).

Python parsing uses stdlib ast.
TypeScript/JavaScript parsing uses lightweight regex-based extraction for
project-wide indexing without requiring language-specific parser packages.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class ParsedSymbol:
    name: str
    qualified_name: str
    symbol_type: str
    file_path: str
    line_start: int
    line_end: int


@dataclass
class ParsedImport:
    source_path: str
    imported_module: str


@dataclass
class RawCall:
    caller_qname: str
    callee_name: str
    arg_names: list[str] = field(default_factory=list)
    result_var_name: str | None = None


@dataclass
class ParsedVariable:
    name: str
    qualified_name: str
    scope_qname: str
    file_path: str
    line_number: int
    role: str


@dataclass
class ParsedVariableFlow:
    source_qname: str
    target_qname: str
    scope_qname: str
    line_number: int
    flow_type: str


@dataclass
class ParsedFile:
    path: str
    language: str
    symbols: list[ParsedSymbol] = field(default_factory=list)
    imports: list[ParsedImport] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    variables: list[ParsedVariable] = field(default_factory=list)
    variable_flows: list[ParsedVariableFlow] = field(default_factory=list)
    parse_error: str | None = None


def path_to_module(file_path: str) -> str:
    p = Path(file_path)
    try:
        rel = p.relative_to(Path(file_path).anchor)
    except ValueError:
        rel = p
    parts = [part for part in rel.parts if part not in ("src",)]
    module = ".".join(parts)
    for suffix in (".py", ".ts", ".tsx", ".js", ".jsx"):
        if module.endswith(suffix):
            module = module[: -len(suffix)]
            break
    return module


class PythonParser:
    """Parse a Python file and return structured symbol/import data."""

    def parse(self, file_path: str) -> ParsedFile:
        path = Path(file_path)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=file_path)
        except SyntaxError as exc:
            return ParsedFile(path=file_path, language="python", parse_error=str(exc))

        module_qname = path_to_module(file_path)
        result = ParsedFile(path=file_path, language="python")

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_qname = f"{module_qname}.{node.name}"
                result.symbols.append(
                    ParsedSymbol(
                        name=node.name,
                        qualified_name=class_qname,
                        symbol_type="class",
                        file_path=file_path,
                        line_start=node.lineno,
                        line_end=node.end_lineno or node.lineno,
                    )
                )
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        kind = "async_method" if isinstance(item, ast.AsyncFunctionDef) else "method"
                        result.symbols.append(
                            ParsedSymbol(
                                name=item.name,
                                qualified_name=f"{class_qname}.{item.name}",
                                symbol_type=kind,
                                file_path=file_path,
                                line_start=item.lineno,
                                line_end=item.end_lineno or item.lineno,
                            )
                        )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent = self._get_parent(tree, node)
                if isinstance(parent, ast.ClassDef):
                    continue
                kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                result.symbols.append(
                    ParsedSymbol(
                        name=node.name,
                        qualified_name=f"{module_qname}.{node.name}",
                        symbol_type=kind,
                        file_path=file_path,
                        line_start=node.lineno,
                        line_end=node.end_lineno or node.lineno,
                    )
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    result.imports.append(ParsedImport(source_path=file_path, imported_module=alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    result.imports.append(ParsedImport(source_path=file_path, imported_module=node.module))

        self._extract_variable_flows(tree, result, module_qname)

        return result

    def _extract_variable_flows(self, tree: ast.AST, result: ParsedFile, module_qname: str) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_qname = f"{module_qname}.{node.name}"
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self._collect_function_variable_flows(item, f"{class_qname}.{item.name}", result)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent = self._get_parent(tree, node)
                if isinstance(parent, ast.ClassDef):
                    continue
                self._collect_function_variable_flows(node, f"{module_qname}.{node.name}", result)

    def _collect_function_variable_flows(
        self,
        func_node: ast.FunctionDef | ast.AsyncFunctionDef,
        scope_qname: str,
        result: ParsedFile,
    ) -> None:
        variables_by_name: dict[str, ParsedVariable] = {}
        seen_flows: set[tuple[str, str, str, int]] = set()

        return_var = self._ensure_variable(result, variables_by_name, scope_qname, "__return__", func_node.lineno, "return")
        param_names: set[str] = set()
        for arg in self._iter_python_args(func_node.args):
            if arg.arg in {"self", "cls"}:
                continue
            param_names.add(arg.arg)
            self._ensure_variable(result, variables_by_name, scope_qname, arg.arg, arg.lineno or func_node.lineno, "parameter")

        for node in ast.walk(func_node):
            if isinstance(node, ast.Assign):
                target_names = self._extract_python_target_names(node.targets)
                source_names = self._extract_python_source_names(node.value)
                self._append_python_flows(
                    result,
                    variables_by_name,
                    scope_qname,
                    target_names,
                    source_names,
                    node.lineno,
                    "assignment",
                    param_names,
                    seen_flows,
                )
            elif isinstance(node, ast.AnnAssign):
                target_names = self._extract_python_target_names([node.target])
                source_names = self._extract_python_source_names(node.value)
                self._append_python_flows(
                    result,
                    variables_by_name,
                    scope_qname,
                    target_names,
                    source_names,
                    node.lineno,
                    "assignment",
                    param_names,
                    seen_flows,
                )
            elif isinstance(node, ast.AugAssign):
                target_names = self._extract_python_target_names([node.target])
                source_names = sorted(set(target_names + self._extract_python_source_names(node.value)))
                self._append_python_flows(
                    result,
                    variables_by_name,
                    scope_qname,
                    target_names,
                    source_names,
                    node.lineno,
                    "assignment",
                    param_names,
                    seen_flows,
                )
            elif isinstance(node, ast.Return) and node.value is not None:
                source_names = self._extract_python_source_names(node.value)
                for source_name in source_names:
                    source_role = "parameter" if source_name in param_names else "local"
                    source_var = self._ensure_variable(
                        result,
                        variables_by_name,
                        scope_qname,
                        source_name,
                        node.lineno,
                        source_role,
                    )
                    key = (source_var.qualified_name, return_var.qualified_name, "return", node.lineno)
                    if key in seen_flows or source_var.qualified_name == return_var.qualified_name:
                        continue
                    seen_flows.add(key)
                    result.variable_flows.append(
                        ParsedVariableFlow(
                            source_qname=source_var.qualified_name,
                            target_qname=return_var.qualified_name,
                            scope_qname=scope_qname,
                            line_number=node.lineno,
                            flow_type="return",
                        )
                    )

    @staticmethod
    def _iter_python_args(args: ast.arguments) -> list[ast.arg]:
        return list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)

    def _append_python_flows(
        self,
        result: ParsedFile,
        variables_by_name: dict[str, ParsedVariable],
        scope_qname: str,
        target_names: list[str],
        source_names: list[str],
        line_number: int,
        flow_type: str,
        param_names: set[str],
        seen_flows: set[tuple[str, str, str, int]],
    ) -> None:
        for target_name in target_names:
            target_var = self._ensure_variable(result, variables_by_name, scope_qname, target_name, line_number, "local")
            for source_name in source_names:
                source_role = "parameter" if source_name in param_names else "local"
                source_var = self._ensure_variable(result, variables_by_name, scope_qname, source_name, line_number, source_role)
                key = (source_var.qualified_name, target_var.qualified_name, flow_type, line_number)
                if key in seen_flows or source_var.qualified_name == target_var.qualified_name:
                    continue
                seen_flows.add(key)
                result.variable_flows.append(
                    ParsedVariableFlow(
                        source_qname=source_var.qualified_name,
                        target_qname=target_var.qualified_name,
                        scope_qname=scope_qname,
                        line_number=line_number,
                        flow_type=flow_type,
                    )
                )

    def _ensure_variable(
        self,
        result: ParsedFile,
        variables_by_name: dict[str, ParsedVariable],
        scope_qname: str,
        var_name: str,
        line_number: int,
        role: str,
    ) -> ParsedVariable:
        existing = variables_by_name.get(var_name)
        if existing is not None:
            if existing.role == "local" and role == "parameter":
                existing.role = role
            return existing
        variable = ParsedVariable(
            name=var_name,
            qualified_name=f"{scope_qname}:{var_name}",
            scope_qname=scope_qname,
            file_path=result.path,
            line_number=line_number,
            role=role,
        )
        variables_by_name[var_name] = variable
        result.variables.append(variable)
        return variable

    @staticmethod
    def _extract_python_target_names(targets: list[ast.AST]) -> list[str]:
        names: set[str] = set()
        for target in targets:
            for node in ast.walk(target):
                if isinstance(node, ast.Name):
                    names.add(node.id)
        return sorted(name for name in names if name not in {"self", "cls"})

    @staticmethod
    def _extract_python_source_names(node: ast.AST | None) -> list[str]:
        if node is None:
            return []
        names: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id not in {"self", "cls"}:
                names.add(child.id)
        return sorted(names)

    @staticmethod
    def _get_parent(tree: ast.AST, target: ast.AST) -> ast.AST | None:
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                if child is target:
                    return node
        return None


class TypeScriptJavaScriptParser:
    """Regex-based parser for basic TS/JS indexing support."""

    _IMPORT_FROM_RE = re.compile(r"^\s*(?:import|export)\b.*?from\s+[\"']([^\"']+)[\"']", re.MULTILINE)
    _IMPORT_SIDE_EFFECT_RE = re.compile(r"^\s*import\s+[\"']([^\"']+)[\"']", re.MULTILINE)
    _REQUIRE_RE = re.compile(r"require\(\s*[\"']([^\"']+)[\"']\s*\)")
    _CLASS_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)", re.MULTILINE)
    _INTERFACE_RE = re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)", re.MULTILINE)
    _TYPE_RE = re.compile(r"^\s*(?:export\s+)?type\s+(\w+)\s*=", re.MULTILINE)
    _ENUM_RE = re.compile(r"^\s*(?:export\s+)?enum\s+(\w+)", re.MULTILINE)
    _FUNCTION_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(", re.MULTILINE)
    _FUNCTION_SCOPE_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^\)]*)\)")
    _ARROW_RE = re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^\)]*\)|\w+)\s*=>",
        re.MULTILINE,
    )
    _ARROW_SCOPE_RE = re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\(([^\)]*)\)|(\w+))\s*=>",
        re.MULTILINE,
    )
    _RETURN_RE = re.compile(r"\breturn\s+(.+?);?$")

    def parse(self, file_path: str) -> ParsedFile:
        path = Path(file_path)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ParsedFile(path=file_path, language=self._language_for(path), parse_error=str(exc))

        module_qname = path_to_module(file_path)
        result = ParsedFile(path=file_path, language=self._language_for(path))
        lines = source.splitlines()

        for imported_module in self._extract_imports(source):
            result.imports.append(ParsedImport(source_path=file_path, imported_module=imported_module))

        self._append_matches(result, lines, module_qname, self._CLASS_RE, "class")
        self._append_matches(result, lines, module_qname, self._INTERFACE_RE, "interface")
        self._append_matches(result, lines, module_qname, self._TYPE_RE, "type")
        self._append_matches(result, lines, module_qname, self._ENUM_RE, "enum")
        self._append_matches(result, lines, module_qname, self._FUNCTION_RE, "function")
        self._append_matches(result, lines, module_qname, self._ARROW_RE, "function")
        self._append_class_methods(result, lines, module_qname)
        self._extract_calls(result, source, lines, module_qname)
        self._extract_variable_flows(result, lines, module_qname)
        return result

    def _append_matches(
        self,
        result: ParsedFile,
        lines: list[str],
        module_qname: str,
        pattern: re.Pattern[str],
        symbol_type: str,
    ) -> None:
        seen: set[tuple[str, int]] = set()
        for lineno, line in enumerate(lines, start=1):
            match = pattern.search(line)
            if not match:
                continue
            name = match.group(1)
            key = (name, lineno)
            if key in seen:
                continue
            seen.add(key)
            result.symbols.append(
                ParsedSymbol(
                    name=name,
                    qualified_name=f"{module_qname}.{name}",
                    symbol_type=symbol_type,
                    file_path=result.path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )

    def _append_class_methods(self, result: ParsedFile, lines: list[str], module_qname: str) -> None:
        class_stack: list[tuple[str, int]] = []
        brace_depth = 0
        method_re = re.compile(r"^\s*(?:async\s+)?(\w+)\s*\([^\)]*\)\s*\{")

        for lineno, line in enumerate(lines, start=1):
            class_match = self._CLASS_RE.search(line)
            if class_match:
                class_name = class_match.group(1)
                current_depth = brace_depth + line.count("{") - line.count("}")
                class_stack.append((class_name, max(1, current_depth)))
            elif class_stack:
                method_match = method_re.search(line)
                if method_match:
                    method_name = method_match.group(1)
                    if method_name != "constructor":
                        class_name = class_stack[-1][0]
                        result.symbols.append(
                            ParsedSymbol(
                                name=method_name,
                                qualified_name=f"{module_qname}.{class_name}.{method_name}",
                                symbol_type="method",
                                file_path=result.path,
                                line_start=lineno,
                                line_end=lineno,
                            )
                        )

            brace_depth += line.count("{") - line.count("}")
            while class_stack and brace_depth < class_stack[-1][1]:
                class_stack.pop()

    def _extract_imports(self, source: str) -> list[str]:
        imports: list[str] = []
        imports.extend(self._IMPORT_FROM_RE.findall(source))
        imports.extend(self._IMPORT_SIDE_EFFECT_RE.findall(source))
        imports.extend(self._REQUIRE_RE.findall(source))
        return imports

    def _extract_calls(self, result: ParsedFile, source: str, lines: list[str], module_qname: str) -> None:
        """Extract function/method calls from TS/JS source."""
        call_re = re.compile(r"(?:^|\s|[({,])(\w+)\s*\(([^)]*)\)")
        assign_call_re = re.compile(r"^\s*(?:(?:const|let|var)\s+)?(\w+)\s*=\s*(\w+)\s*\(([^)]*)\)")
        return_call_re = re.compile(r"^\s*return\s+(\w+)\s*\(([^)]*)\)")
        seen_calls: set[tuple[str, str, int, str | None]] = set()

        class_stack: list[tuple[str, int]] = []
        scope_stack: list[tuple[str, int]] = []
        brace_depth = 0
        method_re = re.compile(r"^\s*(?:async\s+)?(\w+)\s*\(([^\)]*)\)\s*\{")

        for lineno, line in enumerate(lines, start=1):
            class_match = self._CLASS_RE.search(line)
            if class_match:
                class_name = class_match.group(1)
                current_depth = brace_depth + line.count("{") - line.count("}")
                class_stack.append((class_name, max(1, current_depth)))

            function_match = self._FUNCTION_SCOPE_RE.search(line)
            arrow_match = self._ARROW_SCOPE_RE.search(line)
            method_match = method_re.search(line) if class_stack else None
            if function_match:
                scope_stack.append((f"{module_qname}.{function_match.group(1)}", brace_depth + max(1, line.count("{"))))
            elif arrow_match:
                scope_stack.append((f"{module_qname}.{arrow_match.group(1)}", brace_depth + max(1, line.count("{"))))
            elif method_match and method_match.group(1) != "constructor":
                class_name = class_stack[-1][0]
                scope_stack.append((f"{module_qname}.{class_name}.{method_match.group(1)}", brace_depth + max(1, line.count("{"))))

            current_scope = scope_stack[-1][0] if scope_stack else None
            if current_scope:
                assign_match = assign_call_re.search(line)
                return_match = return_call_re.search(line)
                if assign_match:
                    target_name = assign_match.group(1)
                    callee_name = assign_match.group(2)
                    arg_names = self._extract_js_identifiers(assign_match.group(3), excluded=set())
                    key = (current_scope, callee_name, lineno, target_name)
                    if key not in seen_calls:
                        seen_calls.add(key)
                        result.calls.append(
                            RawCall(
                                caller_qname=current_scope,
                                callee_name=callee_name,
                                arg_names=arg_names,
                                result_var_name=target_name,
                            )
                        )
                elif return_match:
                    callee_name = return_match.group(1)
                    arg_names = self._extract_js_identifiers(return_match.group(2), excluded=set())
                    key = (current_scope, callee_name, lineno, "__return__")
                    if key not in seen_calls:
                        seen_calls.add(key)
                        result.calls.append(
                            RawCall(
                                caller_qname=current_scope,
                                callee_name=callee_name,
                                arg_names=arg_names,
                                result_var_name="__return__",
                            )
                        )
                else:
                    for match in call_re.finditer(line):
                        callee_name = match.group(1)
                        if callee_name in {"if", "for", "while", "switch", "catch", "function", "async", "class", "return"}:
                            continue
                        arg_names = self._extract_js_identifiers(match.group(2), excluded=set())
                        key = (current_scope, callee_name, lineno, None)
                        if key not in seen_calls:
                            seen_calls.add(key)
                            result.calls.append(
                                RawCall(
                                    caller_qname=current_scope,
                                    callee_name=callee_name,
                                    arg_names=arg_names,
                                )
                            )

            brace_depth += line.count("{") - line.count("}")
            while class_stack and brace_depth < class_stack[-1][1]:
                class_stack.pop()
            while scope_stack and brace_depth < scope_stack[-1][1]:
                scope_stack.pop()

    def _extract_variable_flows(self, result: ParsedFile, lines: list[str], module_qname: str) -> None:
        class_stack: list[tuple[str, int]] = []
        scope_stack: list[tuple[str, int, set[str]]] = []
        brace_depth = 0
        method_re = re.compile(r"^\s*(?:async\s+)?(\w+)\s*\(([^\)]*)\)\s*\{")
        assign_re = re.compile(r"^\s*(?:(?:const|let|var)\s+)?(\w+)\s*=\s*(.+?);?$")

        for lineno, line in enumerate(lines, start=1):
            class_match = self._CLASS_RE.search(line)
            if class_match:
                class_name = class_match.group(1)
                current_depth = brace_depth + line.count("{") - line.count("}")
                class_stack.append((class_name, max(1, current_depth)))

            function_match = self._FUNCTION_SCOPE_RE.search(line)
            arrow_match = self._ARROW_SCOPE_RE.search(line)
            method_match = method_re.search(line) if class_stack else None
            if function_match:
                scope_qname = f"{module_qname}.{function_match.group(1)}"
                params = self._extract_js_params(function_match.group(2))
                scope_stack.append((scope_qname, brace_depth + max(1, line.count("{")), params))
                self._seed_js_scope_variables(result, scope_qname, params, lineno)
            elif arrow_match:
                scope_qname = f"{module_qname}.{arrow_match.group(1)}"
                param_blob = arrow_match.group(2) or arrow_match.group(3) or ""
                params = self._extract_js_params(param_blob)
                scope_stack.append((scope_qname, brace_depth + max(1, line.count("{")), params))
                self._seed_js_scope_variables(result, scope_qname, params, lineno)
            elif method_match and method_match.group(1) != "constructor":
                class_name = class_stack[-1][0]
                scope_qname = f"{module_qname}.{class_name}.{method_match.group(1)}"
                params = self._extract_js_params(method_match.group(2))
                scope_stack.append((scope_qname, brace_depth + max(1, line.count("{")), params))
                self._seed_js_scope_variables(result, scope_qname, params, lineno)

            if scope_stack:
                scope_qname, _, params = scope_stack[-1]
                assign_match = assign_re.search(line)
                if assign_match and "=>" not in line and not line.lstrip().startswith("return "):
                    target_name = assign_match.group(1)
                    source_names = self._extract_js_identifiers(assign_match.group(2), excluded={target_name})
                    self._append_js_flows(result, scope_qname, target_name, source_names, lineno, "assignment", params)

                return_match = self._RETURN_RE.search(line)
                if return_match:
                    source_names = self._extract_js_identifiers(return_match.group(1), excluded=set())
                    for source_name in source_names:
                        source_role = "parameter" if source_name in params else "local"
                        source_qname = self._ensure_js_variable(result, scope_qname, source_name, lineno, source_role)
                        return_qname = self._ensure_js_variable(result, scope_qname, "__return__", lineno, "return")
                        if source_qname == return_qname:
                            continue
                        result.variable_flows.append(
                            ParsedVariableFlow(
                                source_qname=source_qname,
                                target_qname=return_qname,
                                scope_qname=scope_qname,
                                line_number=lineno,
                                flow_type="return",
                            )
                        )

            brace_depth += line.count("{") - line.count("}")
            while class_stack and brace_depth < class_stack[-1][1]:
                class_stack.pop()
            while scope_stack and brace_depth < scope_stack[-1][1]:
                scope_stack.pop()

    def _seed_js_scope_variables(self, result: ParsedFile, scope_qname: str, params: set[str], lineno: int) -> None:
        self._ensure_js_variable(result, scope_qname, "__return__", lineno, "return")
        for param in sorted(params):
            self._ensure_js_variable(result, scope_qname, param, lineno, "parameter")

    def _append_js_flows(
        self,
        result: ParsedFile,
        scope_qname: str,
        target_name: str,
        source_names: list[str],
        lineno: int,
        flow_type: str,
        params: set[str],
    ) -> None:
        target_qname = self._ensure_js_variable(result, scope_qname, target_name, lineno, "local")
        seen: set[tuple[str, str, str, int]] = {
            (flow.source_qname, flow.target_qname, flow.flow_type, flow.line_number)
            for flow in result.variable_flows
            if flow.scope_qname == scope_qname
        }
        for source_name in source_names:
            source_role = "parameter" if source_name in params else "local"
            source_qname = self._ensure_js_variable(result, scope_qname, source_name, lineno, source_role)
            key = (source_qname, target_qname, flow_type, lineno)
            if key in seen or source_qname == target_qname:
                continue
            seen.add(key)
            result.variable_flows.append(
                ParsedVariableFlow(
                    source_qname=source_qname,
                    target_qname=target_qname,
                    scope_qname=scope_qname,
                    line_number=lineno,
                    flow_type=flow_type,
                )
            )

    def _ensure_js_variable(self, result: ParsedFile, scope_qname: str, name: str, lineno: int, role: str) -> str:
        qualified_name = f"{scope_qname}:{name}"
        for variable in result.variables:
            if variable.qualified_name == qualified_name:
                if variable.role == "local" and role == "parameter":
                    variable.role = role
                return qualified_name
        result.variables.append(
            ParsedVariable(
                name=name,
                qualified_name=qualified_name,
                scope_qname=scope_qname,
                file_path=result.path,
                line_number=lineno,
                role=role,
            )
        )
        return qualified_name

    @staticmethod
    def _extract_js_params(raw_params: str) -> set[str]:
        return {
            token.strip()
            for token in raw_params.split(",")
            if token.strip() and token.strip() not in {"this"}
        }

    @staticmethod
    def _extract_js_identifiers(expr: str, excluded: set[str]) -> list[str]:
        keywords = {
            "return", "new", "await", "true", "false", "null", "undefined", "if", "for", "while",
            "switch", "case", "catch", "try", "const", "let", "var", "function", "class", "async",
            "this",
        }
        identifiers = {
            token
            for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expr)
            if token not in keywords and token not in excluded
        }
        return sorted(identifiers)

    @staticmethod
    def _language_for(path: Path) -> str:
        if path.suffix.lower() in {".ts", ".tsx"}:
            return "typescript"
        return "javascript"


class GoParser:
    """Parse Go source files."""

    _PACKAGE_RE = re.compile(r"package\s+(\w+)")
    _FUNC_RE = re.compile(r"func\s+(\w+)\s*\(")
    _FUNC_SCOPE_RE = re.compile(r"func\s+(\w+)\s*\(([^\)]*)\)")
    _STRUCT_RE = re.compile(r"type\s+(\w+)\s+struct\s*\{")
    _INTERFACE_RE = re.compile(r"type\s+(\w+)\s+interface\s*\{")
    _METHOD_RE = re.compile(r"func\s*\(\s*(\w+)\s+\*?(\w+)\s*\)\s+(\w+)\s*\(")
    _METHOD_SCOPE_RE = re.compile(r"func\s*\(\s*(\w+)\s+\*?(\w+)\s*\)\s+(\w+)\s*\(([^\)]*)\)")
    _IMPORT_RE = re.compile(r'^\s*import\s+"([^"]+)"\s*$')
    _IMPORT_PATH_RE = re.compile(r'"([^"]+)"')

    def parse(self, file_path: str) -> ParsedFile:
        path = Path(file_path)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            lines = source.split("\n")
        except Exception as exc:
            return ParsedFile(path=file_path, language="go", parse_error=str(exc))

        module_qname = path_to_module(file_path)
        result = ParsedFile(path=file_path, language="go")

        # Extract package name
        for lineno, line in enumerate(lines, start=1):
            pkg_match = self._PACKAGE_RE.search(line)
            if pkg_match:
                pkg_name = pkg_match.group(1)
                module_qname = f"{module_qname}.{pkg_name}" if module_qname else pkg_name
                break

        # Extract symbols: structs, interfaces, functions
        self._append_matches(result, lines, module_qname, self._STRUCT_RE, "struct")
        self._append_matches(result, lines, module_qname, self._INTERFACE_RE, "interface")
        self._append_matches(result, lines, module_qname, self._FUNC_RE, "function")
        self._append_methods(result, lines, module_qname)

        # Extract imports
        self._extract_imports(result, source)
        self._extract_variable_flows(result, lines, module_qname)

        return result

    def _append_matches(
        self,
        result: ParsedFile,
        lines: list[str],
        module_qname: str,
        pattern: re.Pattern[str],
        symbol_type: str,
    ) -> None:
        seen: set[tuple[str, int]] = set()
        for lineno, line in enumerate(lines, start=1):
            match = pattern.search(line)
            if not match:
                continue
            name = match.group(1)
            key = (name, lineno)
            if key in seen:
                continue
            seen.add(key)
            result.symbols.append(
                ParsedSymbol(
                    name=name,
                    qualified_name=f"{module_qname}.{name}",
                    symbol_type=symbol_type,
                    file_path=result.path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )

    def _append_methods(self, result: ParsedFile, lines: list[str], module_qname: str) -> None:
        """Extract method definitions (receiver type and method name)."""
        seen: set[tuple[str, str, int]] = set()
        for lineno, line in enumerate(lines, start=1):
            match = self._METHOD_RE.search(line)
            if not match:
                continue
            receiver_type = match.group(2)  # e.g., MyStruct
            method_name = match.group(3)    # e.g., DoSomething
            key = (receiver_type, method_name, lineno)
            if key in seen:
                continue
            seen.add(key)
            result.symbols.append(
                ParsedSymbol(
                    name=method_name,
                    qualified_name=f"{module_qname}.{receiver_type}.{method_name}",
                    symbol_type="method",
                    file_path=result.path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )

    def _extract_imports(self, result: ParsedFile, source: str) -> None:
        """Extract Go imports."""
        in_block = False
        for raw_line in source.splitlines():
            line = raw_line.split("//", 1)[0].strip()
            if not line:
                continue
            if in_block:
                if line.startswith(")"):
                    in_block = False
                    continue
                match = self._IMPORT_PATH_RE.search(line)
            elif line.startswith("import (") or line == "import(":
                in_block = True
                continue
            else:
                match = self._IMPORT_RE.match(line)
            if match:
                result.imports.append(
                    ParsedImport(source_path=result.path, imported_module=match.group(1))
                )

    def _extract_variable_flows(self, result: ParsedFile, lines: list[str], module_qname: str) -> None:
        scope_stack: list[tuple[str, int, set[str]]] = []
        brace_depth = 0
        assign_re = re.compile(r"^\s*(\w+)\s*(?::=|=)\s*(.+)$")
        for lineno, line in enumerate(lines, start=1):
            method_match = self._METHOD_SCOPE_RE.search(line)
            func_match = self._FUNC_SCOPE_RE.search(line) if not method_match else None
            if method_match:
                receiver_type = method_match.group(2)
                method_name = method_match.group(3)
                params = self._extract_go_params(method_match.group(4))
                scope_qname = f"{module_qname}.{receiver_type}.{method_name}"
                scope_stack.append((scope_qname, brace_depth + max(1, line.count("{")), params))
                _seed_scope_variables(result, scope_qname, params, lineno)
            elif func_match:
                scope_qname = f"{module_qname}.{func_match.group(1)}"
                params = self._extract_go_params(func_match.group(2))
                scope_stack.append((scope_qname, brace_depth + max(1, line.count("{")), params))
                _seed_scope_variables(result, scope_qname, params, lineno)

            if scope_stack:
                scope_qname, _, params = scope_stack[-1]
                assign_match = assign_re.search(line.strip())
                if assign_match and not line.strip().startswith("return "):
                    target_name = assign_match.group(1)
                    source_names = _extract_identifiers(assign_match.group(2), excluded={target_name}, keywords={"return", "func", "struct", "interface", "map", "range"})
                    _append_scope_flows(result, scope_qname, target_name, source_names, lineno, "assignment", params)

                if line.strip().startswith("return "):
                    source_names = _extract_identifiers(line.strip()[7:], excluded=set(), keywords={"return"})
                    _append_return_flows(result, scope_qname, source_names, lineno, params)

            brace_depth += line.count("{") - line.count("}")
            while scope_stack and brace_depth < scope_stack[-1][1]:
                scope_stack.pop()

    @staticmethod
    def _extract_go_params(raw_params: str) -> set[str]:
        params: set[str] = set()
        for chunk in raw_params.split(","):
            part = chunk.strip()
            if not part:
                continue
            token = part.split()[0]
            if token not in {"_"}:
                params.add(token)
        return params


class RustParser:
    """Parse Rust source files."""

    _MOD_RE = re.compile(r"mod\s+(\w+)")
    _STRUCT_RE = re.compile(r"struct\s+(\w+)")
    _TRAIT_RE = re.compile(r"trait\s+(\w+)")
    _IMPL_RE = re.compile(r"impl\s*(?:<[A-Za-z0-9_:'\s,&]+>\s*)?(\w+)")
    _FUNC_RE = re.compile(r"fn\s+(\w+)\s*\(")
    _FUNC_SCOPE_RE = re.compile(r"fn\s+(\w+)\s*\(([^\)]*)\)")
    _USE_RE = re.compile(r"use\s+([^\s;]+)")

    def parse(self, file_path: str) -> ParsedFile:
        path = Path(file_path)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            lines = source.split("\n")
        except Exception as exc:
            return ParsedFile(path=file_path, language="rust", parse_error=str(exc))

        module_qname = path_to_module(file_path)
        result = ParsedFile(path=file_path, language="rust")

        # Extract symbols: modules, structs, traits, impl blocks, functions
        self._append_matches(result, lines, module_qname, self._MOD_RE, "module")
        self._append_matches(result, lines, module_qname, self._STRUCT_RE, "struct")
        self._append_matches(result, lines, module_qname, self._TRAIT_RE, "trait")
        self._append_matches(result, lines, module_qname, self._IMPL_RE, "impl")
        self._append_matches(result, lines, module_qname, self._FUNC_RE, "function")

        # Extract imports
        for lineno, line in enumerate(lines, start=1):
            match = self._USE_RE.search(line)
            if match:
                import_path = match.group(1).rstrip(";")
                result.imports.append(
                    ParsedImport(source_path=result.path, imported_module=import_path)
                )

        self._extract_variable_flows(result, lines, module_qname)

        return result

    def _append_matches(
        self,
        result: ParsedFile,
        lines: list[str],
        module_qname: str,
        pattern: re.Pattern[str],
        symbol_type: str,
    ) -> None:
        seen: set[tuple[str, int]] = set()
        for lineno, line in enumerate(lines, start=1):
            match = pattern.search(line)
            if not match:
                continue
            name = match.group(1)
            key = (name, lineno)
            if key in seen:
                continue
            seen.add(key)
            result.symbols.append(
                ParsedSymbol(
                    name=name,
                    qualified_name=f"{module_qname}.{name}",
                    symbol_type=symbol_type,
                    file_path=result.path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )

    def _extract_variable_flows(self, result: ParsedFile, lines: list[str], module_qname: str) -> None:
        scope_stack: list[tuple[str, int, set[str]]] = []
        brace_depth = 0
        assign_re = re.compile(r"^\s*let\s+(?:mut\s+)?(\w+)\s*(?::[^=]+)?=\s*(.+);?$")
        tail_expr_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_\s\+\-\*\./:]*)$")
        for lineno, line in enumerate(lines, start=1):
            func_match = self._FUNC_SCOPE_RE.search(line)
            if func_match:
                scope_qname = f"{module_qname}.{func_match.group(1)}"
                params = self._extract_rust_params(func_match.group(2))
                scope_stack.append((scope_qname, brace_depth + max(1, line.count("{")), params))
                _seed_scope_variables(result, scope_qname, params, lineno)

            if scope_stack:
                scope_qname, _, params = scope_stack[-1]
                stripped = line.strip()
                assign_match = assign_re.search(line)
                if assign_match:
                    target_name = assign_match.group(1)
                    source_names = _extract_identifiers(assign_match.group(2), excluded={target_name}, keywords={"let", "mut", "Some", "None", "Ok", "Err", "return"})
                    _append_scope_flows(result, scope_qname, target_name, source_names, lineno, "assignment", params)
                elif stripped.startswith("return "):
                    source_names = _extract_identifiers(stripped[7:], excluded=set(), keywords={"return"})
                    _append_return_flows(result, scope_qname, source_names, lineno, params)
                elif stripped and not stripped.endswith(";") and stripped not in {"{", "}"} and tail_expr_re.match(stripped):
                    source_names = _extract_identifiers(stripped, excluded=set(), keywords={"if", "else", "match", "loop", "while", "for"})
                    _append_return_flows(result, scope_qname, source_names, lineno, params)

            brace_depth += line.count("{") - line.count("}")
            while scope_stack and brace_depth < scope_stack[-1][1]:
                scope_stack.pop()

    @staticmethod
    def _extract_rust_params(raw_params: str) -> set[str]:
        params: set[str] = set()
        for chunk in raw_params.split(","):
            part = chunk.strip()
            if not part or part in {"&self", "self", "&mut self"}:
                continue
            name = part.split(":")[0].strip().lstrip("&").replace("mut ", "").strip()
            if name and name != "_":
                params.add(name)
        return params


class JavaParser:
    """Parse Java source files."""

    _JAVA_TYPE_TOKEN_RE = r"[A-Za-z_][A-Za-z0-9_]*(?:<[A-Za-z0-9_?,\s]+>)?(?:\[\])?"
    _PACKAGE_RE = re.compile(r"package\s+([a-zA-Z0-9_.]+)\s*;")
    _CLASS_RE = re.compile(r"(?:public|private|protected)?\s*(?:static\s+)?class\s+(\w+)")
    _INTERFACE_RE = re.compile(r"(?:public|private|protected)?\s*interface\s+(\w+)")
    _ENUM_RE = re.compile(r"(?:public|private|protected)?\s*enum\s+(\w+)")
    _METHOD_RE = re.compile(r"(?:public|private|protected)?\s+(?:static\s+)?(?:\w+\s+)+(\w+)\s*\([^)]*\)")
    _METHOD_SCOPE_RE = re.compile(
        r"(?:public|private|protected)?\s+(?:static\s+)?(?:" + _JAVA_TYPE_TOKEN_RE + r"\s+)+(\w+)\s*\(([^)]*)\)"
    )
    _IMPORT_RE = re.compile(r"import\s+([a-zA-Z0-9_.]+)\s*;")

    def parse(self, file_path: str) -> ParsedFile:
        path = Path(file_path)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            lines = source.split("\n")
        except Exception as exc:
            return ParsedFile(path=file_path, language="java", parse_error=str(exc))

        module_qname = path_to_module(file_path)
        result = ParsedFile(path=file_path, language="java")

        # Extract package
        for lineno, line in enumerate(lines, start=1):
            pkg_match = self._PACKAGE_RE.search(line)
            if pkg_match:
                pkg_name = pkg_match.group(1)
                module_qname = pkg_name
                break

        # Extract symbols: classes, interfaces, enums
        self._append_matches(result, lines, module_qname, self._CLASS_RE, "class")
        self._append_matches(result, lines, module_qname, self._INTERFACE_RE, "interface")
        self._append_matches(result, lines, module_qname, self._ENUM_RE, "enum")
        self._append_class_methods(result, lines, module_qname)

        # Extract imports
        for lineno, line in enumerate(lines, start=1):
            match = self._IMPORT_RE.search(line)
            if match:
                import_path = match.group(1)
                result.imports.append(
                    ParsedImport(source_path=result.path, imported_module=import_path)
                )

        self._extract_variable_flows(result, lines, module_qname)

        return result

    def _append_matches(
        self,
        result: ParsedFile,
        lines: list[str],
        module_qname: str,
        pattern: re.Pattern[str],
        symbol_type: str,
    ) -> None:
        seen: set[tuple[str, int]] = set()
        for lineno, line in enumerate(lines, start=1):
            match = pattern.search(line)
            if not match:
                continue
            name = match.group(1)
            key = (name, lineno)
            if key in seen:
                continue
            seen.add(key)
            result.symbols.append(
                ParsedSymbol(
                    name=name,
                    qualified_name=f"{module_qname}.{name}",
                    symbol_type=symbol_type,
                    file_path=result.path,
                    line_start=lineno,
                    line_end=lineno,
                )
            )

    def _append_class_methods(self, result: ParsedFile, lines: list[str], module_qname: str) -> None:
        """Extract methods (simplified: look for method patterns within class context)."""
        class_stack: list[tuple[str, int]] = []
        brace_depth = 0

        for lineno, line in enumerate(lines, start=1):
            class_match = self._CLASS_RE.search(line)
            if class_match:
                class_name = class_match.group(1)
                current_depth = brace_depth + line.count("{") - line.count("}")
                class_stack.append((class_name, current_depth))
            elif class_stack:
                method_match = self._METHOD_RE.search(line)
                if method_match:
                    method_name = method_match.group(1)
                    if method_name not in {"if", "for", "while", "switch", "new"}:
                        class_name = class_stack[-1][0]
                        result.symbols.append(
                            ParsedSymbol(
                                name=method_name,
                                qualified_name=f"{module_qname}.{class_name}.{method_name}",
                                symbol_type="method",
                                file_path=result.path,
                                line_start=lineno,
                                line_end=lineno,
                            )
                        )

            brace_depth += line.count("{") - line.count("}")
            while class_stack and brace_depth < class_stack[-1][1]:
                class_stack.pop()

    def _extract_variable_flows(self, result: ParsedFile, lines: list[str], module_qname: str) -> None:
        class_stack: list[tuple[str, int]] = []
        scope_stack: list[tuple[str, int, set[str]]] = []
        brace_depth = 0
        assign_re = re.compile(r"^\s*(?:" + self._JAVA_TYPE_TOKEN_RE + r"\s+)?(\w+)\s*=\s*(.+);?$")
        for lineno, line in enumerate(lines, start=1):
            class_match = self._CLASS_RE.search(line)
            if class_match:
                class_name = class_match.group(1)
                current_depth = brace_depth + line.count("{") - line.count("}")
                class_stack.append((class_name, current_depth))
            elif class_stack:
                method_match = self._METHOD_SCOPE_RE.search(line)
                if method_match:
                    method_name = method_match.group(1)
                    if method_name not in {"if", "for", "while", "switch", "new"}:
                        class_name = class_stack[-1][0]
                        params = self._extract_java_params(method_match.group(2))
                        scope_qname = f"{module_qname}.{class_name}.{method_name}"
                        scope_stack.append((scope_qname, brace_depth + max(1, line.count("{")), params))
                        _seed_scope_variables(result, scope_qname, params, lineno)

            if scope_stack:
                scope_qname, _, params = scope_stack[-1]
                stripped = line.strip()
                assign_match = assign_re.search(line)
                if assign_match and not stripped.startswith("return "):
                    target_name = assign_match.group(1)
                    source_names = _extract_identifiers(assign_match.group(2), excluded={target_name}, keywords={"return", "new", "this", "null", "true", "false"})
                    _append_scope_flows(result, scope_qname, target_name, source_names, lineno, "assignment", params)
                elif stripped.startswith("return "):
                    source_names = _extract_identifiers(stripped[7:], excluded=set(), keywords={"return", "new", "this"})
                    _append_return_flows(result, scope_qname, source_names, lineno, params)

            brace_depth += line.count("{") - line.count("}")
            while class_stack and brace_depth < class_stack[-1][1]:
                class_stack.pop()
            while scope_stack and brace_depth < scope_stack[-1][1]:
                scope_stack.pop()

    @staticmethod
    def _extract_java_params(raw_params: str) -> set[str]:
        params: set[str] = set()
        for chunk in raw_params.split(","):
            part = chunk.strip()
            if not part:
                continue
            tokens = [token for token in part.split() if token not in {"final"}]
            if tokens:
                params.add(tokens[-1].replace("[]", ""))
        return params


def _seed_scope_variables(result: ParsedFile, scope_qname: str, params: set[str], lineno: int) -> None:
    _ensure_scope_variable(result, scope_qname, "__return__", lineno, "return")
    for param in sorted(params):
        _ensure_scope_variable(result, scope_qname, param, lineno, "parameter")


def _ensure_scope_variable(result: ParsedFile, scope_qname: str, name: str, lineno: int, role: str) -> str:
    qualified_name = f"{scope_qname}:{name}"
    for variable in result.variables:
        if variable.qualified_name == qualified_name:
            if variable.role == "local" and role == "parameter":
                variable.role = role
            return qualified_name
    result.variables.append(
        ParsedVariable(
            name=name,
            qualified_name=qualified_name,
            scope_qname=scope_qname,
            file_path=result.path,
            line_number=lineno,
            role=role,
        )
    )
    return qualified_name


def _append_scope_flows(
    result: ParsedFile,
    scope_qname: str,
    target_name: str,
    source_names: list[str],
    lineno: int,
    flow_type: str,
    params: set[str],
) -> None:
    target_qname = _ensure_scope_variable(result, scope_qname, target_name, lineno, "local")
    seen = {
        (flow.source_qname, flow.target_qname, flow.flow_type, flow.line_number)
        for flow in result.variable_flows
        if flow.scope_qname == scope_qname
    }
    for source_name in source_names:
        source_role = "parameter" if source_name in params else "local"
        source_qname = _ensure_scope_variable(result, scope_qname, source_name, lineno, source_role)
        key = (source_qname, target_qname, flow_type, lineno)
        if key in seen or source_qname == target_qname:
            continue
        seen.add(key)
        result.variable_flows.append(
            ParsedVariableFlow(
                source_qname=source_qname,
                target_qname=target_qname,
                scope_qname=scope_qname,
                line_number=lineno,
                flow_type=flow_type,
            )
        )


def _append_return_flows(
    result: ParsedFile,
    scope_qname: str,
    source_names: list[str],
    lineno: int,
    params: set[str],
) -> None:
    return_qname = _ensure_scope_variable(result, scope_qname, "__return__", lineno, "return")
    seen = {
        (flow.source_qname, flow.target_qname, flow.flow_type, flow.line_number)
        for flow in result.variable_flows
        if flow.scope_qname == scope_qname
    }
    for source_name in source_names:
        source_role = "parameter" if source_name in params else "local"
        source_qname = _ensure_scope_variable(result, scope_qname, source_name, lineno, source_role)
        key = (source_qname, return_qname, "return", lineno)
        if key in seen or source_qname == return_qname:
            continue
        seen.add(key)
        result.variable_flows.append(
            ParsedVariableFlow(
                source_qname=source_qname,
                target_qname=return_qname,
                scope_qname=scope_qname,
                line_number=lineno,
                flow_type="return",
            )
        )


def _extract_identifiers(expr: str, excluded: set[str], keywords: set[str]) -> list[str]:
    identifiers = {
        token
        for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expr)
        if token not in keywords and token not in excluded
    }
    return sorted(identifiers)


class SourceParser:
    """Dispatch parser by file extension."""

    def __init__(self) -> None:
        self._python = PythonParser()
        self._ts_js = TypeScriptJavaScriptParser()
        self._go = GoParser()
        self._rust = RustParser()
        self._java = JavaParser()

    def parse(self, file_path: str) -> ParsedFile:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".py":
            return self._python.parse(file_path)
        if suffix in {".ts", ".tsx", ".js", ".jsx"}:
            return self._ts_js.parse(file_path)
        if suffix == ".go":
            return self._go.parse(file_path)
        if suffix == ".rs":
            return self._rust.parse(file_path)
        if suffix == ".java":
            return self._java.parse(file_path)
        return ParsedFile(path=file_path, language="unknown", parse_error="unsupported extension")


SUPPORTED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java"}


def discover_files(repo_path: str) -> Iterator[str]:
    """Yield absolute paths of all supported source files under *repo_path*."""
    import os

    skip_dirs = {
        ".git", ".venv", "venv", "env", "__pycache__",
        "node_modules", ".mypy_cache", ".pytest_cache", "dist", "build",
        ".next", "coverage",
    }
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        for fname in files:
            fpath = os.path.join(root, fname)
            if Path(fpath).suffix.lower() in SUPPORTED_EXTENSIONS:
                yield fpath
