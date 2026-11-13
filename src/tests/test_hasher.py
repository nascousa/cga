"""Tests for file hash dedup (hasher.py)."""

import ast

import pytest

from backend.indexer.call_analyzer import CallAnalyzer, PythonParameter
from backend.indexer.hasher import sha256_file, file_changed, hash_symbols, hash_calls, hash_imports, hash_variable_flows
from backend.indexer.parser import ParsedFile, ParsedSymbol, ParsedVariable, ParsedVariableFlow, RawCall, ParsedImport


def test_sha256_stable(tmp_path):
    f = tmp_path / "hello.py"
    f.write_text("print('hello')", encoding="utf-8")
    h1 = sha256_file(str(f))
    h2 = sha256_file(str(f))
    assert h1 == h2
    assert len(h1) == 64


def test_sha256_changes_on_edit(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("x = 1", encoding="utf-8")
    h1 = sha256_file(str(f))
    f.write_text("x = 2", encoding="utf-8")
    h2 = sha256_file(str(f))
    assert h1 != h2


def test_file_changed_no_stored():
    assert file_changed("abc", None) is True


def test_file_changed_same():
    assert file_changed("abc", "abc") is False


def test_file_changed_different():
    assert file_changed("abc", "def") is True


def test_hash_symbols_same():
    """Symbols with identical qualified names produce same hash."""
    parsed1 = ParsedFile(path="test.py", language="python")
    parsed1.symbols.append(
        ParsedSymbol("foo", "mod.foo", "function", "test.py", 1, 5)
    )
    parsed1.symbols.append(
        ParsedSymbol("bar", "mod.bar", "class", "test.py", 10, 20)
    )
    
    parsed2 = ParsedFile(path="test.py", language="python")
    parsed2.symbols.append(
        ParsedSymbol("foo", "mod.foo", "function", "test.py", 1, 5)
    )
    parsed2.symbols.append(
        ParsedSymbol("bar", "mod.bar", "class", "test.py", 10, 20)
    )
    
    assert hash_symbols(parsed1) == hash_symbols(parsed2)


def test_hash_symbols_different_on_change():
    """Different symbols produce different hash."""
    parsed1 = ParsedFile(path="test.py", language="python")
    parsed1.symbols.append(
        ParsedSymbol("foo", "mod.foo", "function", "test.py", 1, 5)
    )
    
    parsed2 = ParsedFile(path="test.py", language="python")
    parsed2.symbols.append(
        ParsedSymbol("baz", "mod.baz", "function", "test.py", 1, 5)
    )
    
    assert hash_symbols(parsed1) != hash_symbols(parsed2)


def test_hash_calls_order_independent():
    """Call order doesn't affect hash."""
    parsed1 = ParsedFile(path="test.py", language="python")
    parsed1.calls.append(RawCall("mod.a", "b"))
    parsed1.calls.append(RawCall("mod.c", "d"))
    
    parsed2 = ParsedFile(path="test.py", language="python")
    parsed2.calls.append(RawCall("mod.c", "d"))
    parsed2.calls.append(RawCall("mod.a", "b"))
    
    assert hash_calls(parsed1) == hash_calls(parsed2)


@pytest.mark.parametrize(("first", "second"), [
    ("callee(0, x)", "callee(x, 0)"),
    ("callee(a=x)", "callee(b=x)"),
    ("callee(x)", "callee(x, x)"),
    ("first.callee(x)", "second.callee(x)"),
])
def test_hash_calls_tracks_argument_binding_and_qualified_targets(first, second):
    def parsed(expression):
        result = ParsedFile(path="test.py", language="python")
        result.calls = CallAnalyzer().extract(
            ast.parse(f"def caller(x):\n    return {expression}\n"), "test.py", "mod",
        )
        return result

    assert hash_calls(parsed(first)) != hash_calls(parsed(second))


def test_hash_symbols_tracks_parameter_order_when_source_lines_do_not_change():
    first = ParsedFile(path="test.py", language="python", symbols=[
        ParsedSymbol("callee", "mod.callee", "function", "test.py", 1, 2, [
            PythonParameter("a", "positional_or_keyword"), PythonParameter("b", "positional_or_keyword"),
        ]),
    ])
    second = ParsedFile(path="test.py", language="python", symbols=[
        ParsedSymbol("callee", "mod.callee", "function", "test.py", 1, 2, [
            PythonParameter("b", "positional_or_keyword"), PythonParameter("a", "positional_or_keyword"),
        ]),
    ])

    assert hash_symbols(first) != hash_symbols(second)


def test_hash_imports_dedupes():
    """Duplicate imports are treated as single import."""
    parsed1 = ParsedFile(path="test.py", language="python")
    parsed1.imports.append(ParsedImport("test.py", "utils"))
    parsed1.imports.append(ParsedImport("test.py", "utils"))
    parsed1.imports.append(ParsedImport("test.py", "helpers"))
    
    parsed2 = ParsedFile(path="test.py", language="python")
    parsed2.imports.append(ParsedImport("test.py", "helpers"))
    parsed2.imports.append(ParsedImport("test.py", "utils"))
    
    assert hash_imports(parsed1) == hash_imports(parsed2)


def test_hash_variable_flows_order_independent():
    parsed1 = ParsedFile(path="test.py", language="python")
    parsed1.variables.extend(
        [
            ParsedVariable("arg", "mod.fn:arg", "mod.fn", "test.py", 1, "parameter"),
            ParsedVariable("value", "mod.fn:value", "mod.fn", "test.py", 2, "local"),
        ]
    )
    parsed1.variable_flows.append(
        ParsedVariableFlow("mod.fn:arg", "mod.fn:value", "mod.fn", 2, "assignment")
    )

    parsed2 = ParsedFile(path="test.py", language="python")
    parsed2.variables.extend(
        [
            ParsedVariable("value", "mod.fn:value", "mod.fn", "test.py", 2, "local"),
            ParsedVariable("arg", "mod.fn:arg", "mod.fn", "test.py", 1, "parameter"),
        ]
    )
    parsed2.variable_flows.append(
        ParsedVariableFlow("mod.fn:arg", "mod.fn:value", "mod.fn", 2, "assignment")
    )

    assert hash_variable_flows(parsed1) == hash_variable_flows(parsed2)


def test_file_changed_same():
    assert file_changed("abc", "abc") is False


def test_file_changed_different():
    assert file_changed("abc", "xyz") is True
