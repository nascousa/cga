from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.workbriefing.cold_start import apply_cold_start, build_cold_start_plan
from backend.workbriefing.store import ActivityUpsertResult


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_cold_start_plan_builds_summary_payloads_with_provenance(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/guide.md",
        "# Runtime Guide\n\n```bash\necho skip\n```\n\nCGA runs   the API\nand Relay locally.\n\nSecond paragraph.\n",
    )
    _write(tmp_path, ".adc/knowledge/no-heading.md", "- list item only\n\nPlain knowledge note.\n")

    plan = build_cold_start_plan(tmp_path, project_id="CGA123")

    assert plan.skipped == []
    payloads = {payload["external_id"]: payload for payload in plan.payloads}
    assert sorted(payloads) == ["doc:.adc/knowledge/no-heading.md", "doc:docs/guide.md"]
    guide = payloads["doc:docs/guide.md"]
    assert guide["project_id"] == "CGA123"
    assert guide["event_type"] == "knowledge_import"
    assert guide["title"] == "Runtime Guide"
    assert guide["summary"] == "CGA runs the API and Relay locally."
    assert guide["body_text"] == ""
    assert guide["tags"] == ["cold-start", "doc"]
    assert guide["metadata"]["path"] == "docs/guide.md"
    assert len(guide["metadata"]["sha256"]) == 64
    assert payloads["doc:.adc/knowledge/no-heading.md"]["title"] == "no-heading"
    assert payloads["doc:.adc/knowledge/no-heading.md"]["summary"] == "Plain knowledge note."


def test_cold_start_summary_skips_frontmatter_and_metadata_lines(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/slides.md",
        "---\nmarp: true\ntitle: Deck\n---\n\n# Deck\n\nVersion: 1.0\nDate: 2026-07-01\n\nThe real overview.\n",
    )

    plan = build_cold_start_plan(tmp_path, project_id="CGA123")

    assert plan.payloads[0]["title"] == "Deck"
    assert plan.payloads[0]["summary"] == "The real overview."


def test_cold_start_plan_skips_credentials_and_empty_documents(tmp_path: Path) -> None:
    _write(tmp_path, "docs/secret.md", "# Setup\n\nSet password = hunter2hunter2 before startup.\n")
    _write(tmp_path, "docs/empty.md", "# Only Heading\n")
    _write(tmp_path, "docs/long.md", "# Long\n\n" + ("word " * 200) + "\n")

    plan = build_cold_start_plan(tmp_path, project_id="CGA123")

    assert {item["path"]: item["reason"] for item in plan.skipped} == {
        "docs/empty.md": "no summary paragraph",
        "docs/secret.md": "possible credential in summary",
    }
    assert len(plan.payloads) == 1
    assert len(plan.payloads[0]["summary"]) <= 400
    assert plan.payloads[0]["summary"].endswith("...")
    assert "hunter2" not in str(plan.payloads)


@pytest.mark.asyncio
async def test_apply_cold_start_records_each_payload(tmp_path: Path) -> None:
    _write(tmp_path, "docs/a.md", "# A\n\nAlpha summary.\n")
    _write(tmp_path, "docs/b.md", "# B\n\nBeta summary.\n")
    plan = build_cold_start_plan(tmp_path, project_id="CGA123")
    service = AsyncMock()
    service.record_activity = AsyncMock(
        side_effect=[
            ActivityUpsertResult(operation="created", activity=None),
            ActivityUpsertResult(operation="updated", activity=None),
        ]
    )

    counts = await apply_cold_start(service, plan)

    assert service.record_activity.await_count == 2
    assert counts == {"created": 1, "updated": 1}
