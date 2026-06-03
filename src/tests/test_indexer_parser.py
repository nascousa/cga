"""Unit tests for the Python and TS/JS parsers."""

from __future__ import annotations

import textwrap
import tempfile
import os

import pytest

from backend.indexer.parser import (
    PythonParser,
    SourceParser,
    TypeScriptJavaScriptParser,
    GoParser,
    RustParser,
    JavaParser,
    discover_files,
    SUPPORTED_EXTENSIONS,
)


@pytest.fixture()
def tmp_py(tmp_path):
    """Write a temporary Python file and return its path."""
    def _write(source: str) -> str:
        f = tmp_path / "sample.py"
        f.write_text(textwrap.dedent(source))
        return str(f)
    return _write


def test_parse_function(tmp_py):
    path = tmp_py("""\
        def greet(name: str) -> str:
            return f"Hello {name}"
    """)
    result = PythonParser().parse(path)
    assert result.parse_error is None
    names = [s.name for s in result.symbols]
    assert "greet" in names
    sym = next(s for s in result.symbols if s.name == "greet")
    assert sym.symbol_type == "function"
    assert sym.line_start == 1


def test_parse_async_function(tmp_py):
    path = tmp_py("""\
        async def fetch() -> None:
            pass
    """)
    result = PythonParser().parse(path)
    sym = next(s for s in result.symbols if s.name == "fetch")
    assert sym.symbol_type == "async_function"


def test_parse_class_and_method(tmp_py):
    path = tmp_py("""\
        class Indexer:
            def run(self) -> None:
                pass
            async def arun(self) -> None:
                pass
    """)
    result = PythonParser().parse(path)
    types = {s.name: s.symbol_type for s in result.symbols}
    assert types["Indexer"] == "class"
    assert types["run"] == "method"
    assert types["arun"] == "async_method"


def test_parse_imports(tmp_py):
    path = tmp_py("""\
        import os
        from pathlib import Path
    """)
    result = PythonParser().parse(path)
    modules = [i.imported_module for i in result.imports]
    assert "os" in modules
    assert "pathlib" in modules


def test_parse_python_variable_flows(tmp_py):
    path = tmp_py("""\
        def build(user_id, prefix):
            label = prefix
            result = label
            return result
    """)
    result = PythonParser().parse(path)
    variable_names = {variable.name for variable in result.variables}
    assert {"user_id", "prefix", "label", "result", "__return__"}.issuperset(variable_names)
    flows = {(flow.source_qname.split(":")[-1], flow.target_qname.split(":")[-1], flow.flow_type) for flow in result.variable_flows}
    assert ("prefix", "label", "assignment") in flows
    assert ("label", "result", "assignment") in flows
    assert ("result", "__return__", "return") in flows


def test_parse_syntax_error(tmp_py):
    path = tmp_py("def broken(:\n    pass\n")
    result = PythonParser().parse(path)
    assert result.parse_error is not None
    assert result.symbols == []


def test_discover_files(tmp_path):
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "a.ts").write_text("export function x() {}")
    (tmp_path / "b.txt").write_text("skip")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "c.py").write_text("y = 2")
    (sub / "d.jsx").write_text("export const View = () => <div />")
    skip = tmp_path / "__pycache__"
    skip.mkdir()
    (skip / "e.py").write_text("z = 3")

    found = list(discover_files(str(tmp_path)))
    paths = [os.path.basename(p) for p in found]
    assert "a.py" in paths
    assert "a.ts" in paths
    assert "c.py" in paths
    assert "d.jsx" in paths
    assert "b.txt" not in paths
    assert "e.py" not in paths  # inside __pycache__


def test_parse_typescript_symbols_and_imports(tmp_path):
    path = tmp_path / "sample.ts"
    path.write_text(
        textwrap.dedent(
            """\
            import { foo } from './lib';
            export interface User { id: string }
            export type UserId = string;
            export enum Status { Active = 'active' }
            export class Service {
                run() {
                    return foo();
                }
            }
            export function buildUser() {
                return new Service();
            }
            export const loadUser = async () => {
                return buildUser();
            };
            """
        ),
        encoding="utf-8",
    )

    result = TypeScriptJavaScriptParser().parse(str(path))
    names = {s.name: s.symbol_type for s in result.symbols}
    assert result.language == "typescript"
    assert names["User"] == "interface"
    assert names["UserId"] == "type"
    assert names["Status"] == "enum"
    assert names["Service"] == "class"
    assert names["run"] == "method"
    assert names["buildUser"] == "function"
    assert names["loadUser"] == "function"
    assert "./lib" in [i.imported_module for i in result.imports]
    assert len(result.calls) > 0


def test_parse_javascript_symbols_and_requires(tmp_path):
    path = tmp_path / "sample.js"
    path.write_text(
        textwrap.dedent(
            """\
            const pathUtil = require('path');
            class Worker {
                start() {
                    return true;
                }
            }
            function boot() {
                return new Worker();
            }
            const render = () => boot();
            """
        ),
        encoding="utf-8",
    )

    result = TypeScriptJavaScriptParser().parse(str(path))
    names = {s.name: s.symbol_type for s in result.symbols}
    assert result.language == "javascript"
    assert names["Worker"] == "class"
    assert names["start"] == "method"
    assert names["boot"] == "function"
    assert names["render"] == "function"
    assert "path" in [i.imported_module for i in result.imports]


def test_parse_typescript_variable_flows(tmp_path):
    path = tmp_path / "vars.ts"
    path.write_text(
        textwrap.dedent(
            """\
            export function render(input, suffix) {
                const label = input;
                const finalValue = label;
                return finalValue;
            }
            """
        ),
        encoding="utf-8",
    )

    result = TypeScriptJavaScriptParser().parse(str(path))
    variable_names = {variable.name for variable in result.variables}
    assert {"input", "suffix", "label", "finalValue", "__return__"}.issuperset(variable_names)
    flows = {(flow.source_qname.split(":")[-1], flow.target_qname.split(":")[-1], flow.flow_type) for flow in result.variable_flows}
    assert ("input", "label", "assignment") in flows
    assert ("label", "finalValue", "assignment") in flows
    assert ("finalValue", "__return__", "return") in flows


def test_source_parser_dispatches_by_extension(tmp_path):
    py = tmp_path / "app.py"
    py.write_text("def greet():\n    pass\n", encoding="utf-8")
    ts = tmp_path / "app.ts"
    ts.write_text("export function greet() {}\n", encoding="utf-8")

    parser = SourceParser()
    py_result = parser.parse(str(py))
    ts_result = parser.parse(str(ts))
    assert py_result.language == "python"
    assert ts_result.language == "typescript"


def test_parse_go_file(tmp_path):
    """Test Go parser: functions, structs, interfaces."""
    go_file = tmp_path / "main.go"
    go_file.write_text(
        textwrap.dedent("""\
        package main

        type Person struct {
            Name string
        }

        type Reader interface {
            Read() []byte
        }

        func (p *Person) Greet() string {
            return p.Name
        }

        func main() {
            p := &Person{Name: "Alice"}
            println(p.Greet())
        }
        """),
        encoding="utf-8",
    )

    result = GoParser().parse(str(go_file))
    assert result.language == "go"
    names = {s.name: s.symbol_type for s in result.symbols}
    assert names["Person"] == "struct"
    assert names["Reader"] == "interface"
    assert names["main"] == "function"
    assert names["Greet"] == "method"


def test_parse_go_import_block_with_aliases(tmp_path):
    go_file = tmp_path / "imports.go"
    go_file.write_text(
        textwrap.dedent("""\
        package main

        import (
            "fmt"
            alias "example.com/project/pkg"
            _ "github.com/lib/pq"
        )
        """),
        encoding="utf-8",
    )

    result = GoParser().parse(str(go_file))
    imports = [item.imported_module for item in result.imports]
    assert imports == ["fmt", "example.com/project/pkg", "github.com/lib/pq"]


def test_parse_go_variable_flows(tmp_path):
    go_file = tmp_path / "vars.go"
    go_file.write_text(
        textwrap.dedent("""\
        package main

        func build(input string, suffix string) string {
            label := input
            result := label
            return result
        }
        """),
        encoding="utf-8",
    )
    result = GoParser().parse(str(go_file))
    flows = {(flow.source_qname.split(":")[-1], flow.target_qname.split(":")[-1], flow.flow_type) for flow in result.variable_flows}
    assert ("input", "label", "assignment") in flows
    assert ("label", "result", "assignment") in flows
    assert ("result", "__return__", "return") in flows


def test_parse_rust_file(tmp_path):
    """Test Rust parser: modules, structs, traits, functions."""
    rs_file = tmp_path / "lib.rs"
    rs_file.write_text(
        textwrap.dedent("""\
        pub mod utils;

        pub struct Config {
            path: String,
        }

        pub trait Handler {
            fn handle(&self);
        }

        pub fn parse() {
            println!("parsing");
        }
        """),
        encoding="utf-8",
    )

    result = RustParser().parse(str(rs_file))
    assert result.language == "rust"
    names = {s.name: s.symbol_type for s in result.symbols}
    assert names["utils"] == "module"
    assert names["Config"] == "struct"
    assert names["Handler"] == "trait"
    assert names["parse"] == "function"


def test_parse_rust_impl_with_generics(tmp_path):
    rs_file = tmp_path / "generic.rs"
    rs_file.write_text(
        textwrap.dedent("""\
        pub struct Store<T> {
            value: T,
        }

        impl<T> Store<T> {
            pub fn get(&self) {}
        }
        """),
        encoding="utf-8",
    )

    result = RustParser().parse(str(rs_file))
    names = {s.name: s.symbol_type for s in result.symbols}
    assert names["Store"] == "impl"


def test_parse_rust_variable_flows(tmp_path):
    rs_file = tmp_path / "vars.rs"
    rs_file.write_text(
        textwrap.dedent("""\
        pub fn build(input: String, suffix: String) -> String {
            let label = input;
            let result = label;
            return result;
        }
        """),
        encoding="utf-8",
    )
    result = RustParser().parse(str(rs_file))
    flows = {(flow.source_qname.split(":")[-1], flow.target_qname.split(":")[-1], flow.flow_type) for flow in result.variable_flows}
    assert ("input", "label", "assignment") in flows
    assert ("label", "result", "assignment") in flows
    assert ("result", "__return__", "return") in flows


def test_parse_java_file(tmp_path):
    """Test Java parser: classes, interfaces, methods."""
    java_file = tmp_path / "Main.java"
    java_file.write_text(
        textwrap.dedent("""\
        package com.example;

        import java.io.*;

        public class Main {
            public static void main(String[] args) {
                System.out.println("Hello");
            }

            public String greet(String name) {
                return "Hello " + name;
            }
        }

        interface Runner {
            void run();
        }
        """),
        encoding="utf-8",
    )

    result = JavaParser().parse(str(java_file))
    assert result.language == "java"
    names = {s.name: s.symbol_type for s in result.symbols}
    assert names["Main"] == "class"
    assert names["Runner"] == "interface"
    assert names["main"] == "method"
    assert names["greet"] == "method"


def test_parse_java_variable_flows(tmp_path):
    java_file = tmp_path / "Vars.java"
    java_file.write_text(
        textwrap.dedent("""\
        package com.example;

        public class Vars {
            public String render(String input, String suffix) {
                String label = input;
                String result = label;
                return result;
            }
        }
        """),
        encoding="utf-8",
    )
    result = JavaParser().parse(str(java_file))
    flows = {(flow.source_qname.split(":")[-1], flow.target_qname.split(":")[-1], flow.flow_type) for flow in result.variable_flows}
    assert ("input", "label", "assignment") in flows
    assert ("label", "result", "assignment") in flows
    assert ("result", "__return__", "return") in flows


def test_supported_extensions_include_new_languages():
    """Verify that new language extensions are registered."""
    assert ".go" in SUPPORTED_EXTENSIONS
    assert ".rs" in SUPPORTED_EXTENSIONS
    assert ".java" in SUPPORTED_EXTENSIONS
    assert ".py" in SUPPORTED_EXTENSIONS
    assert ".ts" in SUPPORTED_EXTENSIONS


def test_typescript_call_extraction(tmp_path):
    path = tmp_path / "calls.ts"
    path.write_text(
        textwrap.dedent(
            """\
            function helperA() {
                return 42;
            }
            function helperB() {
                return helperA();
            }
            export class MyClass {
                method1() {
                    return helperB();
                }
                method2() {
                    return this.method1();
                }
            }
            """
        ),
        encoding="utf-8",
    )

    result = TypeScriptJavaScriptParser().parse(str(path))
    call_pairs = [(c.caller_qname, c.callee_name) for c in result.calls]
    assert len(call_pairs) > 0
    assert any(callee == "helperA" for _, callee in call_pairs)
    assert any(callee == "helperB" for _, callee in call_pairs)
