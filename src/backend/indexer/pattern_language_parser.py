"""Bounded structural parsing for languages without bundled Tree-sitter grammars."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from backend.indexer.language_definitions import LanguageDefinition
from backend.indexer.parser import (
    ParsedFile,
    ParsedImport,
    ParsedSymbol,
    ParsedVariable,
    ParsedVariableFlow,
    RawCall,
    path_to_module,
)


@dataclass(frozen=True)
class SymbolPattern:
    pattern: str
    symbol_type: str
    callable: bool = False


@dataclass(frozen=True)
class PatternSpec:
    symbols: tuple[SymbolPattern, ...]
    imports: tuple[str, ...]
    assignments: tuple[str, ...]
    calls: tuple[str, ...]
    returns: tuple[str, ...] = ()
    ignored_calls: frozenset[str] = frozenset()


_SPECS = {
    "visual_basic": PatternSpec(
        symbols=(
            SymbolPattern(r"^\s*(?:(?:Public|Private|Friend|Protected|Partial)\s+)*(?:Class|Structure)\s+(?P<name>[A-Za-z_]\w*)", "class"),
            SymbolPattern(r"^\s*(?:(?:Public|Private|Friend|Protected|Shared|Async|Overridable|Overrides)\s+)*(?:Function|Sub)\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)", "function", True),
        ),
        imports=(r"^\s*Imports\s+(?P<module>[\w.]+)",),
        assignments=(r"(?:\bDim\s+)?(?P<target>[A-Za-z_]\w*)(?:\s+As\s+[^=\r\n]+)?\s*=\s*(?P<value>[^\r\n]+)",),
        calls=(r"\b(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^)]*)\)",),
        returns=(r"\bReturn\s+(?P<value>[^\r\n]+)",),
        ignored_calls=frozenset({"function", "if", "sub"}),
    ),
    "ada": PatternSpec(
        symbols=(
            SymbolPattern(r"^\s*package(?:\s+body)?\s+(?P<name>[A-Za-z_]\w*)\s+is", "module"),
            SymbolPattern(r"^\s*(?:function|procedure)\s+(?P<name>[A-Za-z_]\w*)\s*(?:\((?P<params>[^)]*)\))?", "function", True),
        ),
        imports=(r"^\s*(?:with|use)\s+(?P<module>[\w.]+)\s*;",),
        assignments=(
            r"\b(?P<target>[A-Za-z_]\w*)\s*:\s*[^;:=]+:=\s*(?P<value>[^;]+)",
            r"\b(?P<target>[A-Za-z_]\w*)\s*:=\s*(?P<value>[^;]+)",
        ),
        calls=(r"\b(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^)]*)\)",),
        returns=(r"\breturn\s+(?P<value>[^;]+)",),
        ignored_calls=frozenset({"function", "if", "procedure"}),
    ),
    "cobol": PatternSpec(
        symbols=(
            SymbolPattern(r"^\s*PROGRAM-ID\.\s*(?P<name>[A-Za-z0-9-]+)", "module"),
            SymbolPattern(r"^\s*(?P<name>[A-Za-z][A-Za-z0-9-]*)\.\s*$", "function", True),
        ),
        imports=(r"^\s*COPY\s+(?P<module>[A-Za-z0-9_.-]+)",),
        assignments=(r"\bMOVE\s+(?P<value>[A-Za-z0-9-]+)\s+TO\s+(?P<target>[A-Za-z0-9-]+)",),
        calls=(r"\bCALL\s+[\"']?(?P<name>[A-Za-z0-9-]+)[\"']?(?:\s+USING\s+(?P<args>[^.\r\n]+))?",),
        ignored_calls=frozenset(),
    ),
    "mojo": PatternSpec(
        symbols=(
            SymbolPattern(r"^\s*(?:struct|class)\s+(?P<name>[A-Za-z_]\w*)", "struct"),
            SymbolPattern(r"^\s*(?:fn|def)\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)", "function", True),
        ),
        imports=(r"^\s*(?:from\s+(?P<module>[\w.]+)\s+import|import\s+(?P<module_alt>[\w.]+))",),
        assignments=(r"\b(?:let|var)\s+(?P<target>[A-Za-z_]\w*)(?:\s*:[^=\r\n]+)?\s*=\s*(?P<value>[^\r\n]+)",),
        calls=(r"\b(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^)]*)\)",),
        returns=(r"\breturn\s+(?P<value>[^\r\n]+)",),
        ignored_calls=frozenset({"fn", "if", "struct"}),
    ),
    "cmake": PatternSpec(
        symbols=(
            SymbolPattern(r"^\s*(?:function|macro)\s*\(\s*(?P<name>[A-Za-z_]\w*)(?P<params>[^)]*)\)", "function", True),
        ),
        imports=(r"^\s*(?:include|add_subdirectory|find_package)\s*\(\s*[\"']?(?P<module>[^\s\"')]+)",),
        assignments=(r"\bset\s*\(\s*(?P<target>[A-Za-z_]\w*)\s+(?P<value>[^)]+)\)",),
        calls=(r"^\s*(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^)]*)\)",),
        ignored_calls=frozenset({"add_subdirectory", "find_package", "function", "include", "macro", "set"}),
    ),
    "vyper": PatternSpec(
        symbols=(
            SymbolPattern(r"^\s*(?:struct|interface|event)\s+(?P<name>[A-Za-z_]\w*)", "type"),
            SymbolPattern(r"^\s*def\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)", "function", True),
        ),
        imports=(r"^\s*(?:from\s+(?P<module>[\w.]+)\s+import|import\s+(?P<module_alt>[\w.]+))",),
        assignments=(r"\b(?P<target>[A-Za-z_]\w*)[ \t]*:[ \t]*[^=\r\n]+[ \t]*=[ \t]*(?P<value>[^\r\n]+)",),
        calls=(r"\b(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^)]*)\)",),
        returns=(r"\breturn\s+(?P<value>[^\r\n]+)",),
        ignored_calls=frozenset({"assert", "def", "if", "log"}),
    ),
    "move": PatternSpec(
        symbols=(
            SymbolPattern(r"^\s*module\s+(?P<name>[A-Za-z0-9_:]+)\s*\{", "module"),
            SymbolPattern(r"\bstruct\s+(?P<name>[A-Za-z_]\w*)", "struct"),
            SymbolPattern(r"^\s*(?:(?:public|public\([^)]*\))\s+)?(?:entry\s+)?fun\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)", "function", True),
        ),
        imports=(r"^\s*use\s+(?P<module>[A-Za-z0-9_:]+)",),
        assignments=(r"\blet(?:\s+mut)?\s+(?P<target>[A-Za-z_]\w*)(?:\s*:[^=;]+)?\s*=\s*(?P<value>[^;\r\n]+)",),
        calls=(r"\b(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^)]*)\)",),
        returns=(r"\breturn\s+(?P<value>[^;\r\n]+)",),
        ignored_calls=frozenset({"assert", "fun", "if", "while"}),
    ),
    "cairo": PatternSpec(
        symbols=(
            SymbolPattern(r"^\s*(?:struct|enum|trait)\s+(?P<name>[A-Za-z_]\w*)", "type"),
            SymbolPattern(r"^\s*(?:pub\s+)?fn\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)", "function", True),
        ),
        imports=(r"^\s*use\s+(?P<module>[A-Za-z0-9_:]+)",),
        assignments=(r"\blet(?:\s+mut)?\s+(?P<target>[A-Za-z_]\w*)(?:\s*:[^=;]+)?\s*=\s*(?P<value>[^;\r\n]+)",),
        calls=(r"\b(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^)]*)\)",),
        returns=(r"\breturn\s+(?P<value>[^;\r\n]+)",),
        ignored_calls=frozenset({"assert", "fn", "if"}),
    ),
    "clarity": PatternSpec(
        symbols=(
            SymbolPattern(r"\(define-(?:public|private|read-only)\s+\(\s*(?P<name>[A-Za-z][\w-]*)\s*(?P<params>[^)]*)\)", "function", True),
        ),
        imports=(r"\(use-trait\s+[^\s]+\s+(?P<module>[^\s)]+)",),
        assignments=(r"\(let\s+\(\(\s*(?P<target>[A-Za-z][\w-]*)\s+(?P<value>[A-Za-z][\w-]*)",),
        calls=(r"\((?P<name>[A-Za-z][\w-]*)\s+(?P<args>[^()]*)\)",),
        ignored_calls=frozenset({"begin", "define-private", "define-public", "define-read-only", "let", "ok"}),
    ),
    "cadence": PatternSpec(
        symbols=(
            SymbolPattern(r"^\s*(?:access\([^)]*\)\s+)?(?:contract|resource|struct)\s+(?P<name>[A-Za-z_]\w*)", "type"),
            SymbolPattern(r"^\s*(?:access\([^)]*\)\s+)?fun\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)", "function", True),
        ),
        imports=(r"^\s*import\s+(?P<module>[A-Za-z_]\w*)\s+from\s+[^\s]+",),
        assignments=(r"\b(?:let|var)\s+(?P<target>[A-Za-z_]\w*)(?:\s*:[^=\r\n]+)?\s*=\s*(?P<value>[^;\r\n}]+)",),
        calls=(r"\b(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^)]*)\)",),
        returns=(r"\breturn\s+(?P<value>[^\r\n]+)",),
        ignored_calls=frozenset({"access", "fun", "if"}),
    ),
}

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_VALUE_KEYWORDS = frozenset(
    {"as", "byref", "byval", "const", "dim", "false", "let", "new", "none", "nothing", "true", "var"}
)


class PatternStructuralParser:
    """Extract a conservative structural graph using language-specific declaration forms."""

    def __init__(self, definition: LanguageDefinition) -> None:
        self._definition = definition
        self._spec = _SPECS[definition.language]

    def parse(self, file_path: str) -> ParsedFile:
        try:
            source = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ParsedFile(path=file_path, language=self._definition.language, parse_error=str(exc))

        result = ParsedFile(path=file_path, language=self._definition.language)
        module_qname = path_to_module(file_path)
        self._extract_imports(source, result)
        callable_matches: list[tuple[re.Match[str], str]] = []
        for symbol_pattern in self._spec.symbols:
            for match in re.finditer(symbol_pattern.pattern, source, flags=re.IGNORECASE | re.MULTILINE):
                name = match.group("name")
                if self._ignored_cobol_paragraph(name):
                    continue
                result.symbols.append(
                    ParsedSymbol(
                        name=name,
                        qualified_name=f"{module_qname}.{name}",
                        symbol_type=symbol_pattern.symbol_type,
                        file_path=file_path,
                        line_start=self._line(source, match.start()),
                        line_end=self._line(source, match.end()),
                    )
                )
                if symbol_pattern.callable:
                    callable_matches.append((match, name))

        callable_matches.sort(key=lambda item: item[0].start())
        for index, (match, name) in enumerate(callable_matches):
            body_end = callable_matches[index + 1][0].start() if index + 1 < len(callable_matches) else len(source)
            self._extract_callable(source, match, body_end, name, module_qname, result)
        return result

    def _extract_imports(self, source: str, result: ParsedFile) -> None:
        for pattern in self._spec.imports:
            for match in re.finditer(pattern, source, flags=re.IGNORECASE | re.MULTILINE):
                module = match.groupdict().get("module") or match.groupdict().get("module_alt")
                if module:
                    item = ParsedImport(source_path=result.path, imported_module=module.strip("'\""))
                    if item not in result.imports:
                        result.imports.append(item)

    def _extract_callable(
        self,
        source: str,
        match: re.Match[str],
        body_end: int,
        symbol_name: str,
        module_qname: str,
        result: ParsedFile,
    ) -> None:
        scope_qname = f"{module_qname}.{symbol_name}"
        variables: dict[str, ParsedVariable] = {}
        self._variable(result, variables, scope_qname, "__return__", source, match.start(), "return")
        params = match.groupdict().get("params") or ""
        for segment in re.split(r"[,;\s]+" if self._definition.language == "cmake" else r"[,;]", params):
            identifiers = self._identifiers(segment)
            if not identifiers:
                continue
            name = identifiers[0]
            self._variable(result, variables, scope_qname, name, source, match.start(), "parameter")

        body = source[match.end() : body_end]
        body_offset = match.end()
        for pattern in self._spec.assignments:
            for assignment in re.finditer(pattern, body, flags=re.IGNORECASE | re.MULTILINE):
                target_name = assignment.group("target")
                source_names = self._identifiers(assignment.group("value"))
                self._flows(
                    result,
                    variables,
                    scope_qname,
                    target_name,
                    source_names,
                    source,
                    body_offset + assignment.start(),
                    "assignment",
                )
        for pattern in self._spec.calls:
            for call in re.finditer(pattern, body, flags=re.IGNORECASE | re.MULTILINE):
                callee = call.group("name")
                if callee.lower() in self._spec.ignored_calls:
                    continue
                args = call.groupdict().get("args") or ""
                item = RawCall(
                    caller_qname=scope_qname,
                    callee_name=callee,
                    arg_names=self._identifiers(args),
                )
                if item not in result.calls:
                    result.calls.append(item)
        for pattern in self._spec.returns:
            for returned in re.finditer(pattern, body, flags=re.IGNORECASE | re.MULTILINE):
                self._flows(
                    result,
                    variables,
                    scope_qname,
                    "__return__",
                    self._identifiers(returned.group("value")),
                    source,
                    body_offset + returned.start(),
                    "return",
                )

    def _flows(
        self,
        result: ParsedFile,
        variables: dict[str, ParsedVariable],
        scope_qname: str,
        target_name: str,
        source_names: list[str],
        source: str,
        offset: int,
        flow_type: str,
    ) -> None:
        target = self._variable(result, variables, scope_qname, target_name, source, offset, "local")
        for source_name in source_names:
            source_var = self._variable(result, variables, scope_qname, source_name, source, offset, "local")
            if source_var.qualified_name == target.qualified_name:
                continue
            flow = ParsedVariableFlow(
                source_qname=source_var.qualified_name,
                target_qname=target.qualified_name,
                scope_qname=scope_qname,
                line_number=self._line(source, offset),
                flow_type=flow_type,
            )
            if flow not in result.variable_flows:
                result.variable_flows.append(flow)

    @staticmethod
    def _variable(
        result: ParsedFile,
        variables: dict[str, ParsedVariable],
        scope_qname: str,
        name: str,
        source: str,
        offset: int,
        role: str,
    ) -> ParsedVariable:
        variable = variables.get(name)
        if variable is not None:
            if variable.role == "local" and role == "parameter":
                variable.role = role
            return variable
        variable = ParsedVariable(
            name=name,
            qualified_name=f"{scope_qname}:{name}",
            scope_qname=scope_qname,
            file_path=result.path,
            line_number=PatternStructuralParser._line(source, offset),
            role=role,
        )
        variables[name] = variable
        result.variables.append(variable)
        return variable

    @staticmethod
    def _identifiers(value: str) -> list[str]:
        names = [name for name in _IDENTIFIER.findall(value) if name.lower() not in _VALUE_KEYWORDS]
        if "(" in value and names:
            names = names[1:]
        return list(dict.fromkeys(names))

    def _ignored_cobol_paragraph(self, name: str) -> bool:
        return self._definition.language == "cobol" and name.upper() in {
            "DATA",
            "ENVIRONMENT",
            "IDENTIFICATION",
            "PROCEDURE",
            "WORKING-STORAGE",
        }

    @staticmethod
    def _line(source: str, offset: int) -> int:
        return source.count("\n", 0, offset) + 1
