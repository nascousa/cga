"""Standalone indexing canary: python src\\tests\\indexer_canary.py.

Uses runtime dependencies only, creates fixtures below the working directory,
and never connects to or modifies a graph.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.graph import schema as S
from backend.indexer.parser import path_to_module
from backend.indexer.pipeline import IndexPipeline
from backend.indexer.snapshot import INDEX_FORMAT_VERSION, FileSnapshot, SymbolResolver, parse_snapshot


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    workspace = Path(f".indexer-canary-{uuid.uuid4().hex}")
    workspace.mkdir()
    try:
        dependency = workspace / "dependency.py"
        dependency.write_text("def consume(a=0, b=0):\n    return b\n", encoding="utf-8")
        source = workspace / "source.py"
        source.write_text(
            "from dependency import consume\n"
            "def first(x):\n    def helper(z):\n        return consume(0, z)\n    return helper(x)\n"
            "def second(x):\n    def helper(consume):\n        return consume(x)\n    return x\n"
            "def keyword(x):\n    return consume(b=x)\n"
            "def repeated(x):\n    return consume(x, x)\n"
            "def expression(x, y):\n    return consume(0, x + y)\n"
            "def unpacked(items):\n    return consume(*items)\n"
            "def shadowed(x):\n    consume = x\n    consume(0, x)\n"
            "class Outer:\n    class Inner:\n        def run(self, x):\n"
            "            def helper(z):\n                return consume(b=z)\n"
            "            return helper(x)\n",
            encoding="utf-8",
        )
        parsed = FileSnapshot.deserialize(parse_snapshot(str(source.resolve())).serialize())
        resolver = SymbolResolver([parsed, parse_snapshot(str(dependency.resolve()))], workspace.resolve())
        module = path_to_module(str(source.resolve()))
        target = path_to_module(str(dependency.resolve())) + ".consume"
        identities = {symbol.qualified_name for symbol in parsed.parsed.symbols}
        check({
            f"{module}.first.helper", f"{module}.second.helper",
            f"{module}.Outer.Inner.run.helper",
        } <= identities, "Nested symbol identities collide")
        targets = resolver.call_map(parsed)
        check({
            (call.caller_qname, targets.get(f"{call.caller_qname}:{call.callee_qualifier}"))
            for call in parsed.parsed.calls
        } == {
            (f"{module}.first", f"{module}.first.helper"),
            (f"{module}.first.helper", target),
            (f"{module}.second.helper", None),
            (f"{module}.keyword", target),
            (f"{module}.repeated", target),
            (f"{module}.expression", target),
            (f"{module}.unpacked", target),
            (f"{module}.shadowed", None),
            (f"{module}.Outer.Inner.run", f"{module}.Outer.Inner.run.helper"),
            (f"{module}.Outer.Inner.run.helper", target),
        }, "Call attribution or lexical shadowing is incorrect")
        check(not any(
            variable.qualified_name in {f"{module}.first:z", f"{module}.second.helper:x"}
            for variable in parsed.parsed.variables
        ), "Nested variables leak into the wrong scope")

        class Graph:
            def __init__(self) -> None:
                self.flows: list[dict] = []

            def query(self, cypher: str, params: dict | None = None) -> SimpleNamespace:
                if cypher == S.BATCH_EDGE_VARIABLE_FLOWS:
                    self.flows.extend(params["rows"])
                return SimpleNamespace(result_set=[])

        graph = Graph()
        IndexPipeline(graph)._write_cross_scope_variable_flows(
            parsed.parsed.calls, targets, resolver.parameter_map(parsed, targets),
        )
        edges = {
            (row["source_qname"], row["target_qname"])
            for row in graph.flows if row["flow_type"] == "argument"
        }
        check(edges == {
            (f"{module}.first:x", f"{module}.first.helper:z"),
            (f"{module}.first.helper:z", f"{target}:b"),
            (f"{module}.keyword:x", f"{target}:b"),
            (f"{module}.repeated:x", f"{target}:a"),
            (f"{module}.repeated:x", f"{target}:b"),
            (f"{module}.expression:x", f"{target}:b"),
            (f"{module}.expression:y", f"{target}:b"),
            (f"{module}.Outer.Inner.run:x", f"{module}.Outer.Inner.run.helper:z"),
            (f"{module}.Outer.Inner.run.helper:z", f"{target}:b"),
        }, "Constants, keywords, repeated expressions or unpacking bind incorrectly")
        print(json.dumps({
            "status": "passed",
            "index_format_version": INDEX_FORMAT_VERSION,
            "checks": ["lexical_scopes", "shadowing", "argument_binding", "snapshot_roundtrip"],
        }, sort_keys=True))
    finally:
        shutil.rmtree(workspace)


if __name__ == "__main__":
    main()
