import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.adc import collection, service


def test_rule_snapshot_excludes_runtime_context_and_preserves_exact_text():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        files = {
            ".adc/index.md": "# Project\r\nKeep CRLF\r\n",
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
            file.write_bytes(text.encode("utf-8"))
        snapshot = collection.read_rules(directory)
        assert snapshot == {p: text for p, text in files.items() if collection.is_rule_path(p)}
        assert len(snapshot) == 5
        assert service.digest(snapshot) != service.digest({p: text.replace("\r\n", "\n") for p, text in snapshot.items()})
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


@pytest.mark.parametrize("content", ["Baseline", "Replacement"])
@pytest.mark.parametrize("active", [True, False])
def test_collection_requires_explicit_exemption_decision(content, active):
    state = {"changes": [
        {"path": ".adc/index.md", "kind": "exemption", "reason": "Approved waiver", "active": active},
    ]}
    with pytest.raises(HTTPException, match="Explicit exemption decision"):
        collection.rule_changes(state, {".adc/index.md": "Baseline"}, {".adc/index.md": content}, "Collect")
    assert state["changes"][0]["kind"] == "exemption"


@pytest.mark.asyncio
async def test_exemption_conflict_is_reviewable_and_cannot_commit():
    documents = {".adc/index.md": "Baseline\r\n"}
    state = {"revision": 1, "release": {"id": 2}, "documents": {}, "changes": [
        {"path": ".adc/index.md", "kind": "exemption", "reason": "Approved waiver", "active": True},
    ]}
    conn = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    conn.fetch = AsyncMock(return_value=[{"id": 7, "project_name": "Example", "repo_path": "registered"}])
    conn.fetchval = AsyncMock(return_value=2)
    conn.fetchrow = AsyncMock(return_value=None)
    with patch.object(collection, "read_rules", return_value=documents), \
            patch.object(service, "project_revision", AsyncMock(return_value=state)), \
            patch.object(service, "read_release", AsyncMock(return_value={"documents": documents})), \
            patch.object(service, "save_revision", AsyncMock()) as save:
        item = (await collection.preview(conn))[0]
        assert item["status"] == "blocked"
        assert item["changed_paths"] == item["conflicts"] == [".adc/index.md"]
        assert item["rule_review"] == [{
            "path": ".adc/index.md", "source": "Baseline\r\n", "effective": None,
            "conflict": "Existing exemption requires an explicit decision",
        }]
        conn.fetchrow.return_value = {"repo_path": "registered"}
        body = collection.CollectRequest(
            release_id=2, expected_revision=1, snapshot_sha256=item["snapshot_sha256"], reason="Collect",
        )
        with pytest.raises(HTTPException, match="Explicit exemption decision"):
            await collection.collect(conn, 7, body, "admin")
        save.assert_not_awaited()
