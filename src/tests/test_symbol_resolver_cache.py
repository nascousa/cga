from pathlib import Path

import pytest

from backend.indexer.parser import ParsedFile
from backend.indexer.snapshot import FileSnapshot, SymbolResolver


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
