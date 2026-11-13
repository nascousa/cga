from pathlib import Path

import pytest

from backend.indexer.parser import ParsedFile, path_to_module
from backend.indexer.snapshot import FileSnapshot, SymbolResolver, call_key, parse_snapshot


def snapshot(path: Path, language: str = "python") -> FileSnapshot:
    return FileSnapshot(ParsedFile(path=str(path), language=language), "hash")


@pytest.mark.parametrize("module", [".dep", ".missing"])
def test_repeated_module_lookup_does_not_repeat_filesystem_resolution(tmp_path, monkeypatch, module):
    root = tmp_path.resolve()
    source = root / "source.py"
    dependency = root / "dep.py"
    resolver = SymbolResolver([snapshot(source), snapshot(dependency)], root)
    original = Path.resolve
    calls = []

    def measured(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", measured)
    expected = dependency if module == ".dep" else None
    assert resolver.module_file(str(source), module, "python") == expected
    initial_calls = len(calls)
    assert initial_calls > 0
    for _ in range(50):
        assert resolver.module_file(str(source), module, "python") == expected
    assert len(calls) == initial_calls


def test_module_cache_is_source_language_and_generation_scoped(tmp_path):
    root = tmp_path.resolve()
    first = root / "first" / "source.py"
    second = root / "second" / "source.py"
    py_dep = root / "first" / "dep.py"
    js_dep = root / "first" / ".dep.js"
    other_dep = root / "second" / "dep.py"
    snapshots = [snapshot(p) for p in (first, second, js_dep, other_dep)]
    resolver = SymbolResolver(snapshots, root)
    assert resolver.module_file(str(first), ".dep", "python") is None
    assert resolver.module_file(str(first), ".dep", "javascript") == js_dep
    assert resolver.module_file(str(second), ".dep", "python") == other_dep
    refreshed = SymbolResolver([*snapshots, snapshot(py_dep)], root)
    assert refreshed.module_file(str(first), ".dep", "python") == py_dep
    assert resolver.module_file(str(first), ".dep", "python") is None


def test_nested_helpers_resolve_lexically_and_do_not_escape_their_parent(tmp_path):
    dependency = tmp_path / "wanted.py"
    dependency.write_text("def helper(value):\n    return value\n", encoding="utf-8")
    source = tmp_path / "source.py"
    source.write_text(
        "from wanted import helper as selected\n"
        "def first(value):\n"
        "    def helper():\n        return selected(value)\n    return helper()\n"
        "def second():\n    def helper(selected):\n        return selected()\n"
        "def outside():\n    return helper()\n",
        encoding="utf-8",
    )
    parsed = FileSnapshot.deserialize(parse_snapshot(str(source)).serialize())
    resolver = SymbolResolver([parsed, parse_snapshot(str(dependency))], tmp_path)
    module = path_to_module(str(source))
    target = path_to_module(str(dependency))

    assert {
        (call.caller_qname, resolver.resolve(parsed, call))
        for call in parsed.parsed.calls
    } == {
        (f"{module}.first", f"{module}.first.helper"),
        (f"{module}.first.helper", f"{target}.helper"),
        (f"{module}.second.helper", None),
        (f"{module}.outside", None),
    }


@pytest.mark.parametrize("parameter", ["helper", "*helper", "**helper"])
def test_nested_parameters_shadow_outer_imports(tmp_path, parameter):
    dependency = tmp_path / "wanted.py"
    dependency.write_text("def helper():\n    pass\n", encoding="utf-8")
    source = tmp_path / "source.py"
    source.write_text(
        "from wanted import helper\n"
        f"def outer():\n    def inner({parameter}):\n        helper()\n",
        encoding="utf-8",
    )
    parsed = parse_snapshot(str(source))
    resolver = SymbolResolver([parsed, parse_snapshot(str(dependency))], tmp_path)

    assert len(parsed.parsed.calls) == 1
    assert resolver.call_map(parsed) == {}


def test_enclosing_local_shadows_import_even_when_assigned_after_nested_definition(tmp_path):
    dependency = tmp_path / "wanted.py"
    dependency.write_text("def helper():\n    pass\n", encoding="utf-8")
    source = tmp_path / "source.py"
    source.write_text(
        "from wanted import helper\n"
        "def outer():\n    def inner():\n        helper()\n"
        "    helper = lambda: None\n    inner()\n",
        encoding="utf-8",
    )
    parsed = parse_snapshot(str(source))
    resolver = SymbolResolver([parsed, parse_snapshot(str(dependency))], tmp_path)
    module = path_to_module(str(source))

    assert resolver.call_map(parsed) == {f"{module}.outer:inner": f"{module}.outer.inner"}


def test_class_imports_do_not_leak_into_methods_and_nested_functions_capture_receivers(tmp_path):
    for name in ("wanted", "other"):
        (tmp_path / f"{name}.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    source = tmp_path / "source.py"
    source.write_text(
        "from wanted import helper\n"
        "class Owner:\n    from other import helper\n"
        "    def run(self):\n        helper()\n"
        "        def inner():\n            self.sibling()\n        inner()\n"
        "    def sibling(self):\n        pass\n",
        encoding="utf-8",
    )
    parsed = parse_snapshot(str(source))
    resolver = SymbolResolver(
        [parsed, *(parse_snapshot(str(tmp_path / f"{name}.py")) for name in ("wanted", "other"))],
        tmp_path,
    )
    module = path_to_module(str(source))

    assert resolver.call_map(parsed) == {
        f"{module}.Owner.run:helper": f"{path_to_module(str(tmp_path / 'wanted.py'))}.helper",
        f"{module}.Owner.run:inner": f"{module}.Owner.run.inner",
        f"{module}.Owner.run.inner:self.sibling": f"{module}.Owner.sibling",
    }


@pytest.mark.parametrize("expression", [
    "[helper() for helper in items]",
    "{helper() for helper in items}",
    "{helper(): item for helper in items}",
    "(helper() for helper in items)",
])
def test_comprehension_local_calls_do_not_reuse_the_outside_import_target(tmp_path, expression):
    dependency = tmp_path / "wanted.py"
    dependency.write_text("def helper():\n    pass\n", encoding="utf-8")
    source = tmp_path / "source.py"
    source.write_text(
        "from wanted import helper\n"
        f"def caller(items):\n    helper()\n    result = {expression}\n    helper()\n",
        encoding="utf-8",
    )
    parsed = FileSnapshot.deserialize(parse_snapshot(str(source)).serialize())
    resolver = SymbolResolver([parsed, parse_snapshot(str(dependency))], tmp_path)
    target = path_to_module(str(dependency)) + ".helper"
    assert [resolver.resolve(parsed, call) for call in parsed.parsed.calls] == [target, None, target]
    targets = resolver.call_map(parsed)
    assert call_key(parsed.parsed.calls[1]) not in targets
    assert targets[call_key(parsed.parsed.calls[0])] == target


def test_comprehension_first_iterable_keeps_outer_binding_while_later_iterables_are_local(tmp_path):
    dependency = tmp_path / "wanted.py"
    dependency.write_text("def helper():\n    pass\n", encoding="utf-8")
    source = tmp_path / "source.py"
    source.write_text(
        "from wanted import helper\n"
        "def caller():\n"
        "    return [helper() for helper in helper() for item in helper() if helper()]\n",
        encoding="utf-8",
    )
    parsed = parse_snapshot(str(source))
    resolver = SymbolResolver([parsed, parse_snapshot(str(dependency))], tmp_path)
    assert [resolver.resolve(parsed, call) for call in parsed.parsed.calls] == [
        path_to_module(str(dependency)) + ".helper", None, None, None,
    ]
