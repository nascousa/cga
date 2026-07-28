"""Tree-sitter language registration and structural extraction."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, Protocol

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from backend.indexer.language_definitions import LANGUAGE_DEFINITIONS, LanguageDefinition
from backend.indexer.parser import (
    ParsedFile,
    ParsedImport,
    ParsedSymbol,
    ParsedVariable,
    ParsedVariableFlow,
    RawCall,
    path_to_module,
)

_NAME_NODE_TYPES = frozenset(
    {
        "bareword",
        "command_name",
        "constant",
        "IDENTIFIER",
        "atom",
        "field_identifier",
        "function_name",
        "identifier",
        "long_identifier_or_op",
        "name",
        "property_identifier",
        "property_name",
        "scalar",
        "simple_identifier",
        "type_identifier",
        "varname",
        "value_name",
        "value_pattern",
        "variable",
        "variable_name",
        "var",
        "word",
    }
)
_NAME_FIELDS = ("name", "declarator", "method", "function", "command", "pattern")
_TARGET_FIELDS = ("left", "name", "declarator", "pattern", "variable")
_VALUE_FIELDS = ("right", "value", "body", "expression", "result")
_CALL_TARGET_FIELDS = ("function", "method", "name", "command", "type")
_ARGUMENT_CONTAINER_TYPES = frozenset(
    {
        "argument_list",
        "arguments",
        "call_suffix",
        "command_argument",
        "value_arguments",
    }
)


class RegisteredParser(Protocol):
    def parse(self, file_path: str) -> ParsedFile: ...


class LanguageRegistry:
    """Resolve source extensions to structural parser providers."""

    def __init__(self, definitions: tuple[LanguageDefinition, ...] = LANGUAGE_DEFINITIONS) -> None:
        self._by_language = {definition.language: definition for definition in definitions}
        self._by_extension = {
            extension: definition
            for definition in definitions
            for extension in definition.extensions
        }
        self._by_filename = {
            filename: definition
            for definition in definitions
            for filename in definition.filenames
        }

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset(self._by_extension)

    def parser_for(self, file_path: str) -> RegisteredParser | None:
        path = Path(file_path)
        definition = self._by_filename.get(path.name.lower())
        if definition is None:
            suffix = path.suffix.lower()
            if suffix == ".m":
                definition = self._by_language[self._m_language(path)]
            elif suffix == ".h":
                definition = self._by_language[self._h_language(path)]
            else:
                definition = self._by_extension.get(suffix)
        if definition is None:
            return None
        if definition.parser_kind == "pattern":
            from backend.indexer.pattern_language_parser import PatternStructuralParser

            return PatternStructuralParser(definition)
        return TreeSitterStructuralParser(definition)

    @staticmethod
    def _m_language(path: Path) -> str:
        try:
            prefix = path.read_text(encoding="utf-8", errors="replace")[:8192]
        except OSError:
            return "matlab"
        if re.search(r"(?m)^\s*(?:#\s*import\b|@(?:interface|implementation|protocol|class)\b)", prefix):
            return "objective_c"
        return "matlab"

    @staticmethod
    def _h_language(path: Path) -> str:
        try:
            prefix = path.read_text(encoding="utf-8", errors="replace")[:8192]
        except OSError:
            return "c"
        source = re.sub(r"/\*.*?\*/|//[^\r\n]*", "", prefix, flags=re.DOTALL)
        if re.search(
            r"\b(?:class|namespace|template|typename|constexpr|consteval|constinit|"
            r"noexcept|nullptr|decltype)\b|::|\b(?:public|private|protected)\s*:",
            source,
        ):
            return "cpp"
        return "c"


class TreeSitterStructuralParser:
    """Map a Tree-sitter syntax tree into CGA's parser data model."""

    def __init__(self, definition: LanguageDefinition) -> None:
        self._definition = definition

    def parse(self, file_path: str) -> ParsedFile:
        try:
            source = Path(file_path).read_bytes()
            root = get_parser(self._definition.grammar).parse(source).root_node
        except (LookupError, OSError, ValueError) as exc:
            return ParsedFile(
                path=file_path,
                language=self._definition.language,
                parse_error=str(exc),
            )

        result = ParsedFile(path=file_path, language=self._definition.language)
        module_qname = path_to_module(file_path)
        self._visit(root, source, result, module_qname)
        if root.has_error:
            result.parse_error = "tree-sitter recovered from syntax errors"
        return result

    def _visit(
        self,
        node: Node,
        source: bytes,
        result: ParsedFile,
        scope_qname: str,
    ) -> None:
        if node.type in self._definition.import_nodes:
            imported_module = self._import_name(node, source)
            parsed_import = ParsedImport(source_path=result.path, imported_module=imported_module or "")
            if imported_module and parsed_import not in result.imports:
                result.imports.append(parsed_import)

        if node.type in self._definition.call_nodes:
            call = self._call(node, source, scope_qname)
            if call is not None and call not in result.calls:
                result.calls.append(call)

        child_scope = scope_qname
        symbol_type = self._symbol_type(node)
        if symbol_type is not None:
            name_node = self._find_name_node(node)
            if name_node is not None:
                name = self._identifier(name_node, source)
                qualified_name = f"{scope_qname}.{name}"
                symbol = ParsedSymbol(
                    name=name,
                    qualified_name=qualified_name,
                    symbol_type=symbol_type,
                    file_path=result.path,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                )
                if name and symbol not in result.symbols:
                    result.symbols.append(symbol)
                    child_scope = qualified_name
                    if symbol_type in self._definition.callable_symbol_types:
                        self._extract_callable(
                            node,
                            source,
                            result,
                            qualified_name,
                            name,
                        )

        for child in node.named_children:
            self._visit(child, source, result, child_scope)

    def _symbol_type(self, node: Node) -> str | None:
        symbol_type = self._definition.symbol_types.get(node.type)
        if (
            symbol_type is not None
            and self._definition.language == "zig"
            and node.type == "Decl"
            and not any(candidate.type == "FnProto" for candidate in self._walk(node))
        ):
            return None
        if (
            symbol_type is not None
            and self._definition.language == "r"
            and node.type == "binary_operator"
            and not any(candidate.type == "function_definition" for candidate in node.named_children)
        ):
            return None
        if (
            symbol_type is not None
            and self._definition.language == "ocaml"
            and node.type == "let_binding"
            and not any(candidate.type == "parameter" for candidate in node.named_children)
        ):
            return None
        if (
            symbol_type is not None
            and self._definition.language == "nix"
            and node.type == "binding"
            and (
                (expression := node.child_by_field_name("expression")) is None
                or expression.type != "function_expression"
            )
        ):
            return None
        if (
            symbol_type is not None
            and self._definition.language == "groovy"
            and node.type == "command"
            and not any(child.type == "block" for child in node.named_children)
        ):
            return None
        if (
            symbol_type is not None
            and self._definition.language == "fsharp"
            and node.type == "function_or_value_defn"
            and not any(
                candidate.type == "function_declaration_left"
                for candidate in self._walk(node)
            )
        ):
            return None
        return symbol_type

    def _extract_callable(
        self,
        node: Node,
        source: bytes,
        result: ParsedFile,
        scope_qname: str,
        symbol_name: str,
    ) -> None:
        variables: dict[str, ParsedVariable] = {}
        self._ensure_variable(result, variables, scope_qname, "__return__", node, "return")

        parameter_containers = [
            candidate
            for field_name in ("parameters", "parameter", "formal_parameters")
            if (candidate := node.child_by_field_name(field_name)) is not None
        ]
        if not parameter_containers:
            parameter_containers = [
                candidate
                for candidate in self._walk(node)
                if candidate.type in self._definition.parameter_list_nodes
            ]

        seen_parameters: set[str] = set()
        for container in parameter_containers:
            for parameter in self._walk(container):
                if parameter.type not in self._definition.parameter_nodes:
                    continue
                name_node = self._find_name_node(parameter, prefer_last=True)
                if name_node is None:
                    continue
                name = self._identifier(name_node, source)
                if name in {"", "_", "self", "this", "cls", symbol_name} or name in seen_parameters:
                    continue
                seen_parameters.add(name)
                self._ensure_variable(
                    result,
                    variables,
                    scope_qname,
                    name,
                    name_node,
                    "parameter",
                )

        body_node = node
        if (
            self._definition.language == "dart"
            and node.type in {"function_signature", "method_signature"}
            and node.next_named_sibling is not None
            and node.next_named_sibling.type == "function_body"
        ):
            body_node = node.next_named_sibling
        elif self._definition.language == "r" and node.type == "binary_operator":
            body_node = next(
                (
                    child
                    for child in node.named_children
                    if child.type == "function_definition"
                ),
                node,
            )
        elif self._definition.language == "nix" and node.type == "binding":
            body_node = node.child_by_field_name("expression") or node
            parameter_containers = [
                candidate
                for field_name in ("formals", "universal")
                if (candidate := body_node.child_by_field_name(field_name)) is not None
            ]

        for descendant in self._walk_scope(body_node):
            if descendant.type in self._definition.assignment_nodes:
                target_node, value_node = self._assignment_parts(descendant, source)
                name_node = self._find_name_node(target_node) if target_node is not None else None
                if name_node is not None and value_node is not None:
                    self._append_flows(
                        result,
                        variables,
                        scope_qname,
                        self._identifier(name_node, source),
                        self._expression_names(value_node, source),
                        descendant,
                        "assignment",
                    )
            elif descendant.type in self._definition.return_nodes:
                expression = next(
                    (
                        descendant.child_by_field_name(field_name)
                        for field_name in _VALUE_FIELDS
                        if descendant.child_by_field_name(field_name) is not None
                    ),
                    None,
                )
                if expression is None and descendant.named_children:
                    expression = descendant.named_children[-1]
                if expression is not None:
                    self._append_flows(
                        result,
                        variables,
                        scope_qname,
                        "__return__",
                        self._expression_names(expression, source),
                        descendant,
                        "return",
                    )
            elif descendant.type in self._definition.call_nodes:
                call = self._call(descendant, source, scope_qname)
                if call is not None and call not in result.calls:
                    result.calls.append(call)

    def _walk_scope(self, node: Node) -> Iterator[Node]:
        yield node
        for child in node.named_children:
            child_symbol_type = self._symbol_type(child)
            if child_symbol_type in self._definition.callable_symbol_types:
                continue
            yield from self._walk_scope(child)

    def _assignment_parts(self, node: Node, source: bytes) -> tuple[Node | None, Node | None]:
        if self._definition.language == "groovy" and node.type == "command":
            if not any(child.type == "operators" for child in node.named_children):
                return None, None
            identifiers = [
                candidate
                for candidate in self._walk(node)
                if candidate.type == "identifier"
            ]
            if "=" in self._text(node, source):
                return (
                    identifiers[-2] if len(identifiers) > 1 else None,
                    identifiers[-1] if identifiers else None,
                )
        target = next(
            (
                node.child_by_field_name(field_name)
                for field_name in _TARGET_FIELDS
                if node.child_by_field_name(field_name) is not None
            ),
            None,
        )
        value = next(
            (
                node.child_by_field_name(field_name)
                for field_name in _VALUE_FIELDS
                if node.child_by_field_name(field_name) is not None
            ),
            None,
        )
        if target is None and node.named_children:
            target = next(
                (
                    child
                    for child in node.named_children[:-1]
                    if self._find_name_node(child) is not None
                ),
                node.named_children[0],
            )
        if value is None and len(node.named_children) > 1:
            value = node.named_children[-1]
        return target, value

    def _call(self, node: Node, source: bytes, caller_qname: str) -> RawCall | None:
        if self._import_name(node, source):
            return None
        if (
            self._definition.language == "zig"
            and node.type == "SuffixExpr"
            and not any(child.type == "FnCallArguments" for child in node.named_children)
        ):
            return None
        function = next(
            (
                node.child_by_field_name(field_name)
                for field_name in _CALL_TARGET_FIELDS
                if node.child_by_field_name(field_name) is not None
            ),
            None,
        )
        if self._definition.language == "dart" and node.type == "selector":
            function = node.prev_named_sibling
        if function is None and node.named_children:
            function = node.named_children[0]
        if function is None:
            return None
        name_node = self._find_name_node(function, prefer_last=True)
        if name_node is None:
            return None

        arguments = node.child_by_field_name("arguments")
        argument_nodes = [arguments] if arguments is not None else [
            child
            for child in node.named_children
            if child != function
            and child != node.child_by_field_name("receiver")
            and child != node.child_by_field_name("object")
        ]
        arg_names = list(
            dict.fromkeys(
                name
                for argument_node in argument_nodes
                for name in self._expression_names(argument_node, source)
            )
        )
        return RawCall(
            caller_qname=caller_qname,
            callee_name=self._identifier(name_node, source),
            arg_names=arg_names,
        )

    def _append_flows(
        self,
        result: ParsedFile,
        variables: dict[str, ParsedVariable],
        scope_qname: str,
        target_name: str,
        source_names: list[str],
        node: Node,
        flow_type: str,
    ) -> None:
        target = self._ensure_variable(
            result,
            variables,
            scope_qname,
            target_name,
            node,
            "return" if target_name == "__return__" else "local",
        )
        for source_name in source_names:
            source = self._ensure_variable(
                result,
                variables,
                scope_qname,
                source_name,
                node,
                "local",
            )
            if source.qualified_name == target.qualified_name:
                continue
            flow = ParsedVariableFlow(
                source_qname=source.qualified_name,
                target_qname=target.qualified_name,
                scope_qname=scope_qname,
                line_number=node.start_point[0] + 1,
                flow_type=flow_type,
            )
            if flow not in result.variable_flows:
                result.variable_flows.append(flow)

    @staticmethod
    def _ensure_variable(
        result: ParsedFile,
        variables: dict[str, ParsedVariable],
        scope_qname: str,
        name: str,
        node: Node,
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
            line_number=node.start_point[0] + 1,
            role=role,
        )
        variables[name] = variable
        result.variables.append(variable)
        return variable

    def _expression_names(self, node: Node | None, source: bytes) -> list[str]:
        if node is None:
            return []
        is_call = node.type in self._definition.call_nodes
        if (
            is_call
            and self._definition.language == "zig"
            and node.type == "SuffixExpr"
            and not any(child.type == "FnCallArguments" for child in node.named_children)
        ):
            is_call = False
        if is_call:
            target = next(
                (
                    node.child_by_field_name(field_name)
                    for field_name in _CALL_TARGET_FIELDS
                    if node.child_by_field_name(field_name) is not None
                ),
                node.named_children[0] if node.named_children else None,
            )
            names = [
                name
                for child in node.named_children
                if child != target
                for name in self._expression_names(child, source)
            ]
            return list(dict.fromkeys(names))
        if node.type in _NAME_NODE_TYPES:
            name = self._identifier(node, source)
            return [] if name in {"", "_", "self", "this", "super", "cls"} else [name]
        names = [
            name
            for child in node.named_children
            for name in self._expression_names(child, source)
        ]
        return list(dict.fromkeys(names))

    def _import_name(self, node: Node, source: bytes) -> str | None:
        if node.type not in self._definition.import_nodes or self._definition.import_pattern is None:
            return None
        directive = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
        match = re.search(self._definition.import_pattern, directive, flags=re.MULTILINE)
        if match is None:
            return None
        return match.group("module").strip().strip("'\"<>;").removesuffix(".*").rstrip(".")

    def _find_name_node(self, node: Node | None, prefer_last: bool = False) -> Node | None:
        if node is None:
            return None
        if (
            self._definition.language == "objective_c"
            and node.type in {"method_declaration", "method_definition"}
        ):
            return next(
                (child for child in node.named_children if child.type == "identifier"),
                None,
            )
        if self._definition.language == "perl" and node.type == "function":
            return node
        if node.type in _NAME_NODE_TYPES:
            return node
        if self._definition.language == "groovy" and node.type == "command":
            identifiers = [
                candidate
                for candidate in self._walk(node)
                if candidate.type == "identifier"
            ]
            if len(identifiers) > 1:
                return identifiers[1]
        for field_name in _NAME_FIELDS:
            candidate = node.child_by_field_name(field_name)
            if candidate is not None and candidate != node:
                found = self._find_name_node(candidate, prefer_last=prefer_last)
                if found is not None:
                    return found
        matches = [
            candidate
            for candidate in self._walk(node)
            if candidate is not node and candidate.type in _NAME_NODE_TYPES
        ]
        if not matches:
            return None
        return matches[-1] if prefer_last else matches[0]

    def _identifier(self, node: Node, source: bytes) -> str:
        name = self._text(node, source).strip()
        if name.startswith("${") and name.endswith("}"):
            name = name[2:-1]
        return name.lstrip("$@%&")

    @staticmethod
    def _walk(node: Node) -> Iterator[Node]:
        yield node
        for child in node.named_children:
            yield from TreeSitterStructuralParser._walk(child)

    @staticmethod
    def _text(node: Node, source: bytes) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
