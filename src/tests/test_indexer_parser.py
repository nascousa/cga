"""Unit tests for the Python and TS/JS parsers."""

from __future__ import annotations

import os
import textwrap

import pytest

from backend.indexer.language_catalog import parser_language_ids_for_path
from backend.indexer.parser import (
    PythonParser,
    SourceParser,
    TypeScriptJavaScriptParser,
    PowerShellParser,
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
    (tmp_path / "script.ps1").write_text("function Invoke-Thing { }")
    (tmp_path / "Formatter.kt").write_text("fun build() = Unit")
    (tmp_path / "CMakeLists.txt").write_text("project(formatter)")
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
    assert "script.ps1" in paths
    assert "Formatter.kt" in paths
    assert "CMakeLists.txt" in paths
    assert "c.py" in paths
    assert "d.jsx" in paths
    assert "b.txt" not in paths
    assert "e.py" not in paths  # inside __pycache__


def test_discover_files_excludes_disabled_parser_languages(tmp_path):
    (tmp_path / "service.py").write_text("def run(): pass", encoding="utf-8")
    (tmp_path / "client.ts").write_text("export const run = () => 1", encoding="utf-8")
    (tmp_path / "Formatter.kt").write_text("fun build() = Unit", encoding="utf-8")
    (tmp_path / "CMakeLists.txt").write_text("project(sample)", encoding="utf-8")

    found = list(
        discover_files(
            str(tmp_path),
            disabled_languages=frozenset({"python", "kotlin", "cmake"}),
        )
    )
    paths = {os.path.basename(path) for path in found}

    assert paths == {"client.ts"}


def test_parse_powershell_symbols(tmp_path):
    path = tmp_path / "QuickSearch.Index.ps1"
    path.write_text(
        textwrap.dedent(
            """\
            function Invoke-QuickSearchIndex {
                param([string]$Path)
                return $Path
            }

            class QuickSearchIndexState {
            }
            """
        ),
        encoding="utf-8",
    )
    result = PowerShellParser().parse(str(path))
    assert result.parse_error is None
    assert result.language == "powershell"
    types = {symbol.name: symbol.symbol_type for symbol in result.symbols}
    assert types["Invoke-QuickSearchIndex"] == "function"
    assert types["QuickSearchIndexState"] == "class"


def test_source_parser_dispatches_powershell(tmp_path):
    path = tmp_path / "QuickSearch.Support.ps1"
    path.write_text("function Show-QuickSearchAbout { }", encoding="utf-8")
    result = SourceParser().parse(str(path))
    assert result.language == "powershell"
    assert [symbol.name for symbol in result.symbols] == ["Show-QuickSearchAbout"]


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
    assert ".ps1" in SUPPORTED_EXTENSIONS
    assert ".psm1" in SUPPORTED_EXTENSIONS
    assert ".psd1" in SUPPORTED_EXTENSIONS
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


def test_source_parser_structurally_parses_csharp(tmp_path):
    path = tmp_path / "Formatter.cs"
    path.write_text(
        textwrap.dedent(
            """\
            using System.Text;

            namespace Example.Formatting;

            public class Formatter
            {
                public string Build(string input)
                {
                    var label = input;
                    return Normalize(label);
                }

                private string Normalize(string value)
                {
                    return value.Trim();
                }
            }
            """
        ),
        encoding="utf-8",
    )

    result = SourceParser().parse(str(path))

    assert result.parse_error is None
    assert result.language == "csharp"
    assert {(symbol.name, symbol.symbol_type) for symbol in result.symbols} >= {
        ("Formatter", "class"),
        ("Build", "method"),
        ("Normalize", "method"),
    }
    assert "System.Text" in {item.imported_module for item in result.imports}
    assert any(call.callee_name == "Normalize" for call in result.calls)
    flows = {
        (
            flow.source_qname.split(":")[-1],
            flow.target_qname.split(":")[-1],
            flow.flow_type,
        )
        for flow in result.variable_flows
    }
    assert ("input", "label", "assignment") in flows
    assert ("label", "__return__", "return") in flows


@pytest.mark.parametrize(
    ("filename", "language", "source", "symbol", "imported", "callee", "flow"),
    [
        (
            "formatter.c",
            "c",
            "#include <stdio.h>\nint normalize(int value) { return value; }\n"
            "int build(int input) { int label = input; return normalize(label); }\n",
            "build",
            "stdio.h",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.cpp",
            "cpp",
            "#include <string>\nstd::string normalize(std::string value) { return value; }\n"
            "std::string build(std::string input) { auto label = input; return normalize(label); }\n",
            "build",
            "string",
            "normalize",
            ("input", "label"),
        ),
        (
            "Formatter.kt",
            "kotlin",
            "import java.util.Locale\nfun normalize(value: String): String = value\n"
            "fun build(input: String): String { val label = input; return normalize(label) }\n",
            "build",
            "java.util.Locale",
            "normalize",
            ("input", "label"),
        ),
        (
            "Formatter.scala",
            "scala",
            "import java.util.Locale\ndef normalize(value: String): String = value\n"
            "def build(input: String): String = { val label = input; return normalize(label) }\n",
            "build",
            "java.util.Locale",
            "normalize",
            ("input", "label"),
        ),
        (
            "Formatter.swift",
            "swift",
            "import Foundation\nfunc normalize(_ value: String) -> String { return value }\n"
            "func build(_ input: String) -> String { let label = input; return normalize(label) }\n",
            "build",
            "Foundation",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.rb",
            "ruby",
            "require 'json'\ndef normalize(value)\n return value\nend\n"
            "def build(input)\n label = input\n return normalize(label)\nend\n",
            "build",
            "json",
            "normalize",
            ("input", "label"),
        ),
        (
            "Formatter.php",
            "php",
            "<?php require 'vendor/autoload.php';\n"
            "function normalize(string $value): string { return $value; }\n"
            "function build(string $input): string { $label = $input; return normalize($label); }\n",
            "build",
            "vendor/autoload.php",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.dart",
            "dart",
            "import 'dart:convert';\nString normalize(String value) { return value; }\n"
            "String build(String input) { final label = input; return normalize(label); }\n",
            "build",
            "dart:convert",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.lua",
            "lua",
            "local json = require('json')\nfunction normalize(value) return value end\n"
            "function build(input) local label = input return normalize(label) end\n",
            "build",
            "json",
            "normalize",
            ("input", "label"),
        ),
        (
            "Formatter.pl",
            "perl",
            "use strict; sub normalize { my ($value) = @_; return $value; } "
            "sub build { my ($input) = @_; my $label = $input; return normalize($label); }\n",
            "build",
            "strict",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.sh",
            "bash",
            "source ./helpers.sh\nnormalize() { local value=\"$1\"; echo \"$value\"; }\n"
            "build() { local input=\"$1\"; local label=\"$input\"; normalize \"$label\"; }\n",
            "build",
            "./helpers.sh",
            "normalize",
            ("input", "label"),
        ),
        (
            "Formatter.groovy",
            "groovy",
            "import java.util.Locale\nString normalize(String value) { return value }\n"
            "String build(String input) { def label = input; return normalize(label) }\n",
            "build",
            "java.util.Locale",
            "normalize",
            ("input", "label"),
        ),
        (
            "Formatter.fs",
            "fsharp",
            "open System\nlet normalize value = value\n"
            "let build input =\n    let label = input\n    normalize label\n",
            "build",
            "System",
            "normalize",
            ("input", "label"),
        ),
    ],
)
def test_mainstream_structural_language_matrix(
    tmp_path,
    filename,
    language,
    source,
    symbol,
    imported,
    callee,
    flow,
):
    path = tmp_path / filename
    path.write_text(source, encoding="utf-8")

    result = SourceParser().parse(str(path))

    assert result.language == language
    assert symbol in {item.name for item in result.symbols}
    assert imported in {item.imported_module for item in result.imports}
    assert callee in {item.callee_name for item in result.calls}
    flows = {
        (item.source_qname.split(":")[-1], item.target_qname.split(":")[-1])
        for item in result.variable_flows
    }
    assert flow in flows


@pytest.mark.parametrize(
    ("filename", "language", "source", "symbol", "imported", "callee", "flow"),
    [
        (
            "formatter.zig",
            "zig",
            'const std = @import("std");\nfn normalize(value: i32) i32 { return value; }\n'
            "pub fn build(input: i32) i32 { const label = input; return normalize(label); }\n",
            "build",
            "std",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.nim",
            "nim",
            "import strutils\nproc normalize(value: string): string =\n  return value\n"
            "proc build(input: string): string =\n  let label = input\n  return normalize(label)\n",
            "build",
            "strutils",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.d",
            "d",
            "import std.string;\nstring normalize(string value) { return value; }\n"
            "string build(string input) { auto label = input; return normalize(label); }\n",
            "build",
            "std.string",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.f90",
            "fortran",
            "module formatter_mod\n use iso_fortran_env\ncontains\n"
            " function normalize(value) result(out)\n integer :: value, out\n out = value\n end function normalize\n"
            " function build(input) result(out)\n integer :: input, label, out\n label = input\n"
            " out = normalize(label)\n end function build\nend module formatter_mod\n",
            "build",
            "iso_fortran_env",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.pas",
            "pascal",
            "program Formatter; uses SysUtils; function Normalize(Value: Integer): Integer; "
            "begin Normalize := Value; end; function Build(Input: Integer): Integer; "
            "var LabelValue: Integer; begin LabelValue := Input; Build := Normalize(LabelValue); end; begin end.\n",
            "Build",
            "SysUtils",
            "Normalize",
            ("Input", "LabelValue"),
        ),
        (
            "formatter.r",
            "r",
            "library(jsonlite)\nnormalize <- function(value) { return(value) }\n"
            "build <- function(input) { label <- input; return(normalize(label)) }\n",
            "build",
            "jsonlite",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.jl",
            "julia",
            "using JSON\nnormalize(value) = value\nfunction build(input)\n label = input\n"
            " return normalize(label)\nend\n",
            "build",
            "JSON",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.m",
            "matlab",
            "function result = build(input)\nlabel = input;\nresult = normalize(label);\nend\n"
            "function result = normalize(value)\nresult = value;\nend\n",
            "build",
            "helpers",
            "normalize",
            ("input", "label"),
        ),
        (
            "Formatter.hs",
            "haskell",
            "module Formatter where\nimport Data.Text\nnormalize value = value\n"
            "build input = let label = input in normalize label\n",
            "build",
            "Data.Text",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.ml",
            "ocaml",
            "open String\nlet normalize value = value\n"
            "let build input = let label = input in normalize label\n",
            "build",
            "String",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.erl",
            "erlang",
            "-module(formatter).\n-import(string,[trim/1]).\nnormalize(Value) -> Value.\n"
            "build(Input) -> Label = Input, normalize(Label).\n",
            "build",
            "string",
            "normalize",
            ("Input", "Label"),
        ),
    ],
)
def test_system_scientific_functional_language_matrix(
    tmp_path,
    filename,
    language,
    source,
    symbol,
    imported,
    callee,
    flow,
):
    path = tmp_path / filename
    if language == "matlab":
        source = "import helpers.*\n" + source
    path.write_text(source, encoding="utf-8")

    result = SourceParser().parse(str(path))

    assert result.language == language
    assert symbol in {item.name for item in result.symbols}
    assert imported in {item.imported_module for item in result.imports}
    assert callee in {item.callee_name for item in result.calls}
    flows = {
        (item.source_qname.split(":")[-1], item.target_qname.split(":")[-1])
        for item in result.variable_flows
    }
    assert flow in flows


@pytest.mark.parametrize(
    ("filename", "language", "source", "symbol", "imported", "callee", "flow"),
    [
        (
            "Formatter.mm",
            "objective_c",
            "#import <Foundation/Foundation.h>\n@implementation Formatter\n"
            "- (NSString *)normalize:(NSString *)value { return value; }\n"
            "- (NSString *)build:(NSString *)input { NSString *label = input; return [self normalize:label]; }\n@end\n",
            "build",
            "Foundation/Foundation.h",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.cr",
            "crystal",
            "require \"json\"\ndef normalize(value)\n value\nend\n"
            "def build(input)\n label = input\n normalize(label)\nend\n",
            "build",
            "json",
            "normalize",
            ("input", "label"),
        ),
        (
            "Formatter.vb",
            "visual_basic",
            "Imports System.Text\nModule Formatter\nFunction Normalize(value As String) As String\nReturn value\nEnd Function\n"
            "Function Build(input As String) As String\nDim label As String = input\nReturn Normalize(label)\nEnd Function\nEnd Module\n",
            "Build",
            "System.Text",
            "Normalize",
            ("input", "label"),
        ),
        (
            "formatter.adb",
            "ada",
            "with Ada.Text_IO;\npackage body Formatter is\n"
            "function Normalize(Value : Integer) return Integer is begin return Value; end Normalize;\n"
            "function Build(Input : Integer) return Integer is Label : Integer := Input; begin return Normalize(Label); end Build;\nend Formatter;\n",
            "Build",
            "Ada.Text_IO",
            "Normalize",
            ("Input", "Label"),
        ),
        (
            "formatter.cob",
            "cobol",
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. FORMATTER.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n01 LABEL-VALUE PIC X.\n"
            "PROCEDURE DIVISION.\nCOPY HELPERS.\nBUILD.\nMOVE INPUT-VALUE TO LABEL-VALUE\nCALL 'NORMALIZE' USING LABEL-VALUE.\nSTOP RUN.\n",
            "BUILD",
            "HELPERS.",
            "NORMALIZE",
            ("INPUT-VALUE", "LABEL-VALUE"),
        ),
        (
            "formatter.mojo",
            "mojo",
            "from utils import String\nfn normalize(value: String) -> String:\n    return value\n"
            "fn build(input: String) -> String:\n    let label = input\n    return normalize(label)\n",
            "build",
            "utils",
            "normalize",
            ("input", "label"),
        ),
        (
            "CMakeLists.txt",
            "cmake",
            "include(Helpers)\nfunction(normalize value)\n  set(result ${value})\nendfunction()\n"
            "function(build input)\n  set(label ${input})\n  normalize(${label})\nendfunction()\n",
            "build",
            "Helpers",
            "normalize",
            ("input", "label"),
        ),
    ],
)
def test_offline_fallback_language_matrix(
    tmp_path,
    filename,
    language,
    source,
    symbol,
    imported,
    callee,
    flow,
):
    path = tmp_path / filename
    path.write_text(source, encoding="utf-8")

    result = SourceParser().parse(str(path))

    assert result.parse_error is None
    assert result.language == language
    assert symbol in {item.name for item in result.symbols}
    assert imported in {item.imported_module for item in result.imports}
    assert callee in {item.callee_name for item in result.calls}
    flows = {
        (item.source_qname.split(":")[-1], item.target_qname.split(":")[-1])
        for item in result.variable_flows
    }
    assert flow in flows


@pytest.mark.parametrize(
    (
        "filename",
        "language",
        "source",
        "symbols",
        "imports",
        "calls",
        "flow",
    ),
    [
        (
            "Formatter.sol",
            "solidity",
            'import "./Math.sol"; contract Formatter { '
            "function normalize(uint value) public returns (uint) { return value; } "
            "function build(uint input) public returns (uint) { uint label = input; return normalize(label); } }\n",
            {"Formatter", "normalize", "build"},
            {"./Math.sol"},
            {"normalize"},
            ("input", "label"),
        ),
        (
            "formatter.sql",
            "sql",
            "CREATE TABLE items (value INTEGER); "
            "CREATE FUNCTION normalize(value INTEGER) RETURNS INTEGER AS 'SELECT value' LANGUAGE SQL; "
            "SELECT normalize(value) FROM items;\n",
            {"items", "normalize"},
            set(),
            {"normalize"},
            None,
        ),
        (
            "formatter.graphql",
            "graphql",
            "type Query { normalize(value: String!): String } "
            "query Build($input: String!) { normalize(value: $input) }\n",
            {"Query", "Build"},
            set(),
            {"normalize"},
            None,
        ),
        (
            "formatter.proto",
            "protobuf",
            'syntax = "proto3"; import "google/protobuf/empty.proto"; '
            "message Request { string value = 1; } "
            "service Formatter { rpc Normalize(Request) returns (Request); }\n",
            {"Request", "Formatter", "Normalize"},
            {"google/protobuf/empty.proto"},
            set(),
            None,
        ),
        (
            "formatter.bzl",
            "starlark",
            'load("//tools:defs.bzl", "helper")\n'
            "def normalize(value):\n    return value\n"
            "def build(input):\n    label = input\n    return normalize(label)\n",
            {"normalize", "build"},
            {"//tools:defs.bzl"},
            {"normalize"},
            ("input", "label"),
        ),
        (
            "formatter.nix",
            "nix",
            "{ pkgs ? import <nixpkgs> {} }: let normalize = value: value; "
            'build = input: let label = input; in normalize label; in { result = build "raw"; }\n',
            {"normalize", "build"},
            {"nixpkgs"},
            {"normalize", "build"},
            ("input", "label"),
        ),
        (
            "formatter.scss",
            "scss",
            '@use "theme"; @function normalize($value) { @return $value; } '
            "@function build($input) { $label: $input; @return normalize($label); }\n",
            {"normalize", "build"},
            {"theme"},
            {"normalize"},
            ("input", "label"),
        ),
    ],
)
def test_native_domain_language_matrix(
    tmp_path,
    filename,
    language,
    source,
    symbols,
    imports,
    calls,
    flow,
):
    path = tmp_path / filename
    path.write_text(source, encoding="utf-8")

    result = SourceParser().parse(str(path))

    assert result.parse_error is None
    assert result.language == language
    assert symbols <= {item.name for item in result.symbols}
    assert imports <= {item.imported_module for item in result.imports}
    assert calls <= {item.callee_name for item in result.calls}
    if flow is not None:
        flows = {
            (item.source_qname.split(":")[-1], item.target_qname.split(":")[-1])
            for item in result.variable_flows
        }
        assert flow in flows


@pytest.mark.parametrize(
    ("filename", "language", "source", "symbol", "imported", "callee", "flow"),
    [
        (
            "formatter.vy",
            "vyper",
            "from ethereum.ercs import IERC20\n"
            "def normalize(value: uint256) -> uint256:\n    return value\n"
            "def build(input: uint256) -> uint256:\n    label: uint256 = input\n    return self.normalize(label)\n",
            "build",
            "ethereum.ercs",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.move",
            "move",
            "module app::formatter {\nuse std::string;\nfun normalize(value: u64): u64 { value }\n"
            "public fun build(input: u64): u64 { let label = input; normalize(label) }\n}\n",
            "build",
            "std::string",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.cairo",
            "cairo",
            "use core::array;\nfn normalize(value: felt252) -> felt252 { return value; }\n"
            "pub fn build(input: felt252) -> felt252 { let label = input; return normalize(label); }\n",
            "build",
            "core::array",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.clar",
            "clarity",
            "(use-trait formatter-trait .traits.formatter)\n"
            "(define-private (normalize (value uint)) value)\n"
            "(define-public (build (input uint)) (let ((label input)) (ok (normalize label))))\n",
            "build",
            ".traits.formatter",
            "normalize",
            ("input", "label"),
        ),
        (
            "formatter.cdc",
            "cadence",
            "import FormatterUtils from 0x01\n"
            "access(all) fun normalize(value: Int): Int { return value }\n"
            "access(all) fun build(input: Int): Int { let label = input; return normalize(label) }\n",
            "build",
            "FormatterUtils",
            "normalize",
            ("input", "label"),
        ),
    ],
)
def test_blockchain_fallback_language_matrix(
    tmp_path,
    filename,
    language,
    source,
    symbol,
    imported,
    callee,
    flow,
):
    path = tmp_path / filename
    path.write_text(source, encoding="utf-8")

    result = SourceParser().parse(str(path))

    assert result.parse_error is None
    assert result.language == language
    assert symbol in {item.name for item in result.symbols}
    assert imported in {item.imported_module for item in result.imports}
    assert callee in {item.callee_name for item in result.calls}
    flows = {
        (item.source_qname.split(":")[-1], item.target_qname.split(":")[-1])
        for item in result.variable_flows
    }
    assert flow in flows


def test_m_extension_disambiguates_objective_c_and_matlab(tmp_path):
    objective_c_dir = tmp_path / "objc"
    objective_c_dir.mkdir()
    objective_c = objective_c_dir / "Formatter.m"
    objective_c.write_text(
        "#import <Foundation/Foundation.h>\n@implementation Formatter\n"
        "- (NSString *)build:(NSString *)input { return input; }\n@end\n",
        encoding="utf-8",
    )
    matlab_dir = tmp_path / "matlab"
    matlab_dir.mkdir()
    matlab = matlab_dir / "formatter.m"
    matlab.write_text(
        "function result = build(input)\nresult = input;\nend\n",
        encoding="utf-8",
    )

    assert SourceParser().parse(str(objective_c)).language == "objective_c"
    assert SourceParser().parse(str(matlab)).language == "matlab"


def test_h_extension_disambiguates_c_and_cpp(tmp_path):
    c_header = tmp_path / "formatter_c.h"
    c_header.write_text(
        "typedef struct Formatter { int value; } Formatter;\n"
        "int formatter_build(Formatter *formatter);\n",
        encoding="utf-8",
    )
    cpp_header = tmp_path / "formatter_cpp.h"
    cpp_header.write_text(
        "namespace formatter {\n"
        "class Formatter { public: int build() const; };\n"
        "}\n",
        encoding="utf-8",
    )

    assert parser_language_ids_for_path(c_header) == frozenset({"c", "cpp"})
    assert SourceParser().parse(str(c_header)).language == "c"
    assert SourceParser().parse(str(cpp_header)).language == "cpp"
