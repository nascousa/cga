"""Call-graph edge extractor using stdlib ast.

For every function/method in a parsed file, walks the AST to find all
direct function/method call sites and returns (caller_qname, callee_name)
pairs.  These are later resolved to qualified names in the pipeline and
written as CALLS edges in FalkorDB.

Named scopes and argument dependencies use lexical identities shared with the
parser and snapshot resolver. Dynamic receivers and argument unpacking remain
unresolved rather than implying type inference.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Iterator, Literal


PythonScopeNode = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
ParameterKind = Literal[
    "positional_only", "positional_or_keyword", "var_positional", "keyword_only", "var_keyword",
]
MethodBinding = Literal["instance", "class", "static"]


@dataclass
class PythonParameter:
    name: str
    kind: ParameterKind
    has_default: bool = False


@dataclass
class CallArgument:
    """One argument slot with lexically qualified variable dependencies."""

    dependencies: list[str]
    position: int | None = None
    keyword: str | None = None
    unpacked: bool = False


@dataclass
class RawCall:
    """Call evidence; structured arguments supersede legacy flattened arg_names."""

    caller_qname: str
    callee_name: str     # simple name; resolution happens in pipeline
    arg_names: list[str] | None = None
    result_var_name: str | None = None
    callee_qualifier: str | None = None
    arguments: list[CallArgument] | None = None
    result_var_qname: str | None = None
    callee_is_shadowed: bool = False


def walk_python_scope_context(
    node: ast.AST, *, evaluate_annotations: bool = True,
    bound_names: frozenset[str] = frozenset(),
) -> Iterator[tuple[ast.AST, frozenset[str]]]:
    """Walk one named scope while tracking comprehension-local bindings."""
    roots: list[ast.AST] = (
        list(node.body) if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        else [node]
    )
    pending = [(root, bound_names) for root in reversed(roots)]
    while pending:
        current, bound = pending.pop()
        yield current, bound
        if isinstance(current, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            local = bound | frozenset(
                name.id for generator in current.generators
                for name in ast.walk(generator.target) if isinstance(name, ast.Name)
            )
            children = []
            for index, generator in enumerate(current.generators):
                children.extend([
                    (generator.iter, bound if index == 0 else local),
                    (generator.target, local),
                    *((condition, local) for condition in generator.ifs),
                ])
            values = [current.key, current.value] if isinstance(current, ast.DictComp) else [current.elt]
            children.extend((value, local) for value in values)
            pending.extend(reversed(children))
            continue
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            expressions: list[ast.AST] = [
                *current.args.defaults,
                *(default for default in current.args.kw_defaults if default is not None),
            ]
            if not isinstance(current, ast.Lambda):
                expressions.extend(current.decorator_list)
                if evaluate_annotations:
                    arguments = [
                        *current.args.posonlyargs, *current.args.args, *current.args.kwonlyargs,
                        *([current.args.vararg] if current.args.vararg else []),
                        *([current.args.kwarg] if current.args.kwarg else []),
                    ]
                    expressions.extend(argument.annotation for argument in arguments if argument.annotation is not None)
                    if current.returns is not None:
                        expressions.append(current.returns)
        elif isinstance(current, ast.ClassDef):
            expressions = [*current.bases, *current.keywords, *current.decorator_list]
        else:
            expressions = list(ast.iter_child_nodes(current))
        pending.extend((child, bound) for child in reversed(expressions))


def walk_python_scope(node: ast.AST, *, evaluate_annotations: bool = True) -> Iterator[ast.AST]:
    """Walk definition-time expressions without entering nested named bodies."""
    for child, _ in walk_python_scope_context(node, evaluate_annotations=evaluate_annotations):
        yield child


def python_parameters(args: ast.arguments) -> list[PythonParameter]:
    positional = [*args.posonlyargs, *args.args]
    first_default = len(positional) - len(args.defaults)
    parameters = [
        PythonParameter(
            argument.arg,
            "positional_only" if index < len(args.posonlyargs) else "positional_or_keyword",
            index >= first_default,
        )
        for index, argument in enumerate(positional)
    ]
    if args.vararg:
        parameters.append(PythonParameter(args.vararg.arg, "var_positional"))
    parameters.extend(
        PythonParameter(argument.arg, "keyword_only", default is not None)
        for argument, default in zip(args.kwonlyargs, args.kw_defaults)
    )
    if args.kwarg:
        parameters.append(PythonParameter(args.kwarg.arg, "var_keyword"))
    return parameters


@dataclass
class PythonScope:
    node: PythonScopeNode
    qualified_name: str
    parent: PythonScope | None = None
    bound_names: set[str] = field(default_factory=set)
    global_names: set[str] = field(default_factory=set)
    nonlocal_names: set[str] = field(default_factory=set)
    evaluate_annotations: bool = True

    @property
    def lexical_parent(self) -> PythonScope | None:
        parent = self.parent
        while parent is not None and isinstance(parent.node, ast.ClassDef):
            parent = parent.parent
        return parent

    @property
    def method_binding(self) -> MethodBinding | None:
        if not isinstance(self.node, (ast.FunctionDef, ast.AsyncFunctionDef)) or (
            self.parent is None or not isinstance(self.parent.node, ast.ClassDef)
        ):
            return None
        decorators = {
            decorator.id for decorator in self.node.decorator_list
            if isinstance(decorator, ast.Name)
        }
        if "staticmethod" in decorators:
            return "static"
        return "class" if "classmethod" in decorators else "instance"

    def variable_scope(self, name: str) -> PythonScope:
        scope = self
        if name in self.global_names:
            while scope.parent is not None:
                scope = scope.parent
            return scope
        if name not in self.nonlocal_names and name in self.bound_names:
            return self
        while (parent := scope.lexical_parent) is not None:
            scope = parent
            if name in scope.global_names:
                while scope.parent is not None:
                    scope = scope.parent
                break
            if name in scope.bound_names:
                break
        return scope

    def variable_qname(self, name: str) -> str:
        return f"{self.variable_scope(name).qualified_name}:{name}"


def python_scopes(tree: ast.AST, module_qname: str) -> list[PythonScope]:
    if not isinstance(tree, ast.Module):
        raise TypeError("Python scope extraction requires a module AST")
    scopes: list[PythonScope] = []
    evaluate_annotations = not any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )

    def collect(node: PythonScopeNode, qualified_name: str, parent: PythonScope | None) -> None:
        scope = PythonScope(node, qualified_name, parent, evaluate_annotations=evaluate_annotations)
        scopes.append(scope)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope.bound_names.update(parameter.name for parameter in python_parameters(node.args))
        for item, bound in walk_python_scope_context(node, evaluate_annotations=evaluate_annotations):
            if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                scope.bound_names.add(item.name)
                collect(item, f"{qualified_name}.{item.name}", scope)
            elif isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del)):
                if item.id not in bound:
                    scope.bound_names.add(item.id)
            elif isinstance(item, ast.Import):
                scope.bound_names.update(alias.asname or alias.name.split(".")[0] for alias in item.names)
            elif isinstance(item, ast.ImportFrom):
                scope.bound_names.update(alias.asname or alias.name for alias in item.names if alias.name != "*")
            elif isinstance(item, ast.ExceptHandler) and item.name:
                scope.bound_names.add(item.name)
            elif isinstance(item, (ast.MatchAs, ast.MatchStar)) and item.name:
                scope.bound_names.add(item.name)
            elif isinstance(item, ast.MatchMapping) and item.rest:
                scope.bound_names.add(item.rest)
            elif isinstance(item, ast.Global):
                scope.global_names.update(item.names)
            elif isinstance(item, ast.Nonlocal):
                scope.nonlocal_names.update(item.names)
        scope.bound_names.difference_update(scope.global_names | scope.nonlocal_names)

    collect(tree, module_qname, None)
    return scopes


class CallAnalyzer:
    """Extract CALLS relationships from a Python file's AST."""

    def extract(
        self,
        tree: ast.AST,
        file_path: str,
        module_qname: str,
    ) -> list[RawCall]:
        calls: list[RawCall] = []
        parent_map = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for scope in python_scopes(tree, module_qname):
            if isinstance(scope.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._collect_calls(scope, calls, parent_map)
        return calls

    def _collect_calls(
        self,
        scope: PythonScope,
        out: list[RawCall],
        parent_map: dict[ast.AST, ast.AST],
    ) -> None:
        for node, bound in walk_python_scope_context(
            scope.node, evaluate_annotations=scope.evaluate_annotations,
        ):
            if not isinstance(node, ast.Call):
                continue
            name = self._extract_call_name(node)
            if name:
                result_name = self._extract_result_var_name(node, parent_map)
                qualifier = ast.unparse(node.func)
                out.append(
                    RawCall(
                        caller_qname=scope.qualified_name,
                        callee_name=name,
                        arg_names=self._extract_arg_names(node, bound),
                        result_var_name=result_name,
                        callee_qualifier=qualifier,
                        arguments=self._extract_arguments(node, scope, bound),
                        result_var_qname=(
                            f"{scope.qualified_name}:__return__" if result_name == "__return__"
                            else scope.variable_qname(result_name) if result_name else None
                        ),
                        callee_is_shadowed=qualifier.split(".", 1)[0] in bound,
                    )
                )

    @staticmethod
    def _extract_call_name(call: ast.Call) -> str | None:
        if isinstance(call.func, ast.Name):
            return call.func.id
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        return None
    @staticmethod
    def _extract_arg_names(call: ast.Call, bound_names: frozenset[str] = frozenset()) -> list[str]:
        names: list[str] = []
        for arg in list(call.args) + [kw.value for kw in call.keywords if kw.value is not None]:
            for node, bound in walk_python_scope_context(arg, bound_names=bound_names):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in bound:
                    names.append(node.id)
        return list(dict.fromkeys(names))

    @staticmethod
    def _extract_arguments(
        call: ast.Call, scope: PythonScope, bound_names: frozenset[str] = frozenset(),
    ) -> list[CallArgument]:
        def dependencies(expression: ast.AST) -> list[str]:
            return sorted({
                scope.variable_qname(node.id)
                for node, bound in walk_python_scope_context(expression, bound_names=bound_names)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in bound
            })

        return [
            CallArgument(dependencies(argument), position=index, unpacked=isinstance(argument, ast.Starred))
            for index, argument in enumerate(call.args)
        ] + [
            CallArgument(dependencies(keyword.value), keyword=keyword.arg, unpacked=keyword.arg is None)
            for keyword in call.keywords
        ]

    @staticmethod
    def _extract_result_var_name(call: ast.Call, parent_map: dict[ast.AST, ast.AST]) -> str | None:
        parent = parent_map.get(call)
        if isinstance(parent, ast.Assign):
            for target in parent.targets:
                if isinstance(target, ast.Name):
                    return target.id
        if isinstance(parent, ast.AnnAssign) and isinstance(parent.target, ast.Name):
            return parent.target.id
        if isinstance(parent, ast.Return):
            return "__return__"
        return None
