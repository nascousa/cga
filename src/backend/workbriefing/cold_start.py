"""One-time cold-start import of repository documents into Work Briefing memory.

Only a heading-derived title, a short first-paragraph summary and provenance (path, SHA-256)
are recorded. Raw document bodies are never stored, and summaries that look like credentials are skipped.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from backend.workbriefing.store import ActivityUpsertResult

DEFAULT_PATTERNS: tuple[str, ...] = ("docs/**/*.md", ".adc/knowledge/**/*.md")
MAX_SUMMARY_CHARS = 400
_CREDENTIAL_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\s*[:=]\s*\S{4,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"
)


class _ActivityRecorder(Protocol):
    async def record_activity(self, payload: dict[str, Any]) -> ActivityUpsertResult:
        ...


@dataclass
class ColdStartPlan:
    payloads: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


_METADATA_LINE_RE = re.compile(r"^\*{0,2}[A-Za-z][\w ()/-]{0,40}:\*{0,2}\s+\S")


def _strip_frontmatter(text: str) -> str:
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return "\n".join(lines[index + 1 :])
    return text


def _title_and_summary(text: str, fallback_title: str) -> tuple[str, str]:
    title = fallback_title
    paragraphs: list[list[str]] = [[]]
    in_code = False
    for line in _strip_frontmatter(text.replace("\r\n", "\n")).split("\n"):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            paragraphs.append([])
            continue
        if in_code:
            continue
        heading = re.match(r"^#\s+(.+?)\s*#*$", stripped)
        if heading and title == fallback_title:
            title = heading.group(1).strip()
            paragraphs.append([])
            continue
        if not stripped or stripped.startswith(("#", "|", "-", "*", "+", ">", "<!--")) or re.match(r"^\d+[.)]\s", stripped):
            paragraphs.append([])
            continue
        paragraphs[-1].append(stripped)
    first = next(
        (
            " ".join(" ".join(p).split())
            for p in paragraphs
            if p and not all(_METADATA_LINE_RE.match(line) for line in p)
        ),
        "",
    )
    if len(first) > MAX_SUMMARY_CHARS:
        first = first[: MAX_SUMMARY_CHARS - 3].rstrip() + "..."
    return title, first


def build_cold_start_plan(
    root: Path,
    project_id: str,
    patterns: tuple[str, ...] = DEFAULT_PATTERNS,
) -> ColdStartPlan:
    root = Path(root).resolve()
    plan = ColdStartPlan()
    seen: set[Path] = set()
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            resolved = path.resolve()
            if not path.is_file() or resolved in seen or not resolved.is_relative_to(root):
                continue
            seen.add(resolved)
            relative = resolved.relative_to(root).as_posix()
            data = resolved.read_bytes()
            title, summary = _title_and_summary(data.decode("utf-8", errors="replace"), resolved.stem)
            if not summary:
                plan.skipped.append({"path": relative, "reason": "no summary paragraph"})
                continue
            if _CREDENTIAL_RE.search(summary) or _CREDENTIAL_RE.search(title):
                plan.skipped.append({"path": relative, "reason": "possible credential in summary"})
                continue
            modified = datetime.fromtimestamp(resolved.stat().st_mtime, tz=timezone.utc)
            plan.payloads.append(
                {
                    "project_id": project_id,
                    "event_type": "knowledge_import",
                    "external_id": f"doc:{relative}",
                    "title": title[:200],
                    "summary": summary,
                    "body_text": "",
                    "tags": ["cold-start", "doc"],
                    "occurred_at": modified.isoformat().replace("+00:00", "Z"),
                    "metadata": {"path": relative, "sha256": hashlib.sha256(data).hexdigest()},
                }
            )
    return plan


async def apply_cold_start(service: _ActivityRecorder, plan: ColdStartPlan) -> dict[str, int]:
    counts: dict[str, int] = {}
    for payload in plan.payloads:
        result = await service.record_activity(payload)
        counts[result.operation] = counts.get(result.operation, 0) + 1
    return counts
