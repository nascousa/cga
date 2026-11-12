import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backend.adc import collection


def test_rule_snapshot_excludes_runtime_context_and_preserves_exact_text():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        files = {
            ".adc/index.md": "# Project\n",
            ".adc/.codexrules": "Read the approved rules.",
            ".adc/standards/conventions/test.md": "Test first.\n",
            ".adc/rules/custom.md": "Local policy.",
            ".adc/.github/copilot-instructions.md": "Rule pointer.",
            ".adc/cga-relay/mcp/credentials.env": "not-a-rule",
            ".adc/contextgraph-edge-agent/settings.json": "not-a-rule",
            ".adc/planning/status.md": "work in progress",
            ".adc/knowledge/notes.md": "project context",
            ".adc/adc-lock.json": "{}",
        }
        for path, text in files.items():
            file = root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text, encoding="utf-8")
        snapshot = collection.read_rules(directory)
        assert snapshot == {p: text for p, text in files.items() if collection.is_rule_path(p)}
        assert len(snapshot) == 5
        with patch("backend.adc.collection.os.walk", side_effect=lambda *a, **k: k["onerror"](PermissionError("denied"))):
            with pytest.raises(HTTPException, match="denied"):
                collection.read_rules(directory)
        with patch.object(Path, "is_symlink", return_value=True):
            with pytest.raises(HTTPException, match="must not be a link"):
                collection.read_rules(directory)


def test_collection_retains_unrelated_changes_and_removes_redundant_overrides():
    baseline = {".adc/index.md": "Master", ".adc/standards/security.md": "Secure"}
    state = {"changes": [
        {"path": ".adc/index.md", "kind": "override", "content": "Old", "reason": "Old", "active": True},
        {"path": ".adc/knowledge/local.md", "kind": "amendment", "content": "Keep", "reason": "Knowledge", "active": True},
        {"path": ".adc/standards/security.md", "kind": "exemption", "reason": "Keep exception", "active": True},
    ]}
    changes = collection.rule_changes(state, baseline, {
        ".adc/index.md": "Master", ".adc/rules/new.md": "Project-only",
    }, "Collect reviewed source")
    by_path = {c.path: c for c in changes}
    assert ".adc/index.md" not in by_path
    assert by_path[".adc/knowledge/local.md"].content == "Keep"
    assert by_path[".adc/standards/security.md"].kind == "exemption"
    assert by_path[".adc/rules/new.md"].kind == "amendment"
