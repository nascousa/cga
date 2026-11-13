"""Collect registered checkouts' rule snapshots into immutable ADC revisions."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fastapi import HTTPException
from pydantic import Field, field_validator

from backend.adc import service
from backend.indexer.paths import resolve_repo_root


def is_rule_path(path: str) -> bool:
    parts = path.split("/")
    if len(parts) < 2 or parts[0] != ".adc":
        return False
    if len(parts) == 2:
        return parts[1] in {"index.md", "bootstrap.md", "prompt-rules.md"} or parts[1].endswith(("rules", ".rules"))
    return parts[1] in {"standards", "rules", ".github"}


def read_rules(repo_path: str, *, allow_absent: bool = False) -> dict[str, str]:
    def walk_error(error: OSError):
        raise error

    try:
        root = resolve_repo_root(repo_path)
        adc = root / ".adc"
        if adc.is_symlink() or adc.is_junction():
            raise HTTPException(409, "ADC rule directory must not be a link")
        if not adc.is_dir():
            if allow_absent and not adc.exists():
                return {}
            raise HTTPException(409, "Registered checkout has no .adc directory")
        documents = {}
        for directory, dirs, files in os.walk(adc, followlinks=False, onerror=walk_error):
            parent = Path(directory)
            for name in list(dirs):
                child = parent / name
                relative = child.relative_to(root).as_posix()
                if parent == adc and name not in {"standards", "rules", ".github"}:
                    dirs.remove(name)
                elif child.is_symlink() or child.is_junction():
                    raise HTTPException(409, f"Rule directory must not be a link: {relative}")
            for name in sorted(files):
                file = parent / name
                relative = file.relative_to(root).as_posix()
                if not is_rule_path(relative):
                    continue
                if file.is_symlink() or file.is_junction():
                    raise HTTPException(409, f"Rule file must not be a link: {relative}")
                if not file.is_file():
                    raise HTTPException(409, f"Rule path must be a regular file: {relative}")
                if file.stat().st_size > 512_000:
                    raise HTTPException(413, f"Rule file exceeds 512 KB: {relative}")
                with file.open(encoding="utf-8-sig", newline="") as stream:
                    documents[relative] = stream.read()
                if len(documents) > 500 or len(service.canonical(documents).encode()) > 5_000_000:
                    raise HTTPException(413, "Rule snapshot exceeds 500 files or 5 MB")
        service.ReleaseCreate.validate_documents(documents)
        return dict(sorted(documents.items()))
    except (OSError, UnicodeError, ValueError) as error:
        raise HTTPException(409, f"Cannot collect registered project rules: {error}") from error


def exemption_conflicts(state: dict, documents: dict[str, str]) -> list[str]:
    return sorted(c["path"] for c in state["changes"]
                  if c["kind"] == "exemption" and c["path"] in documents)


def rule_changes(state: dict, baseline: dict, documents: dict[str, str], reason: str) -> list[service.Change]:
    conflicts = exemption_conflicts(state, documents)
    if conflicts:
        raise HTTPException(409, "Explicit exemption decision required before collection: " + ", ".join(conflicts))
    changes = {c["path"]: service.Change.model_validate({k: v for k, v in c.items() if k != "active"})
               for c in state["changes"]}
    for path, content in documents.items():
        if baseline.get(path) == content:
            changes.pop(path, None)
        else:
            changes[path] = service.Change(
                path=path, kind="override" if path in baseline else "amendment",
                content=content, reason=reason,
            )
    return list(changes.values())


class CollectRequest(service.StrictModel):
    release_id: int = Field(gt=0)
    expected_revision: int = Field(ge=0)
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=4000)
    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        return service.ReleaseCreate.nonblank(value)


async def preview(conn) -> list[dict]:
    projects = await conn.fetch("SELECT id,project_name,repo_path FROM projects WHERE is_active=1 ORDER BY id")
    latest = await conn.fetchval("SELECT id FROM adc_releases ORDER BY major DESC,minor DESC,patch DESC LIMIT 1")
    results = []
    for project in projects:
        item = {"project_id": project["id"], "project_name": project["project_name"]}
        try:
            if not project["repo_path"]:
                raise HTTPException(409, "Register this project's repository path before collection")
            documents = read_rules(project["repo_path"], allow_absent=True)
            state = await service.project_revision(conn, project["id"])
            if not documents:
                if not state["release"]:
                    raise HTTPException(409, "No local .adc rules; adopt a central ADC baseline first")
                item.update(status="no_local_rules", release_id=state["release"]["id"],
                            note="No local .adc directory; the pinned baseline is managed centrally.")
                results.append(item)
                continue
            release_id = state["release"]["id"] if state["release"] else latest
            if release_id is None:
                raise HTTPException(409, "Publish an ADC baseline before collection")
            effective = state["documents"] if state["release"] else (await service.read_release(conn, release_id))["documents"]
            conflicts = exemption_conflicts(state, documents)
            sha = service.digest(documents)
            previous = await conn.fetchrow(
                """SELECT revision,snapshot_sha256 FROM adc_rule_collections
                   WHERE project_id=$1 ORDER BY revision DESC LIMIT 1""", project["id"])
            matching = all(state["documents"].get(p) == c for p, c in documents.items())
            item.update(
                status="managed" if previous and previous["snapshot_sha256"] == sha and matching else "ready",
                release_id=release_id, expected_revision=state["revision"], snapshot_sha256=sha,
                document_count=len(documents),
                changed_paths=[p for p, c in documents.items() if effective.get(p) != c],
                documents={p: hashlib.sha256(c.encode()).hexdigest() for p, c in documents.items()},
                conflicts=conflicts,
                rule_review=[
                    {"path": p, "source": c, "effective": effective.get(p),
                     "conflict": "Existing exemption requires an explicit decision" if p in conflicts else None}
                    for p, c in documents.items() if effective.get(p) != c or p in conflicts
                ],
            )
            if conflicts:
                item.update(status="blocked", error="Explicit exemption decision required before collection: " + ", ".join(conflicts))
        except HTTPException as error:
            item.update(status="blocked", error=str(error.detail))
        results.append(item)
    return results


async def collect(conn, project_id: int, body: CollectRequest, actor: str):
    async with conn.transaction():
        project = await conn.fetchrow(
            "SELECT repo_path FROM projects WHERE id=$1 AND is_active=1 FOR UPDATE", project_id)
        if project is None:
            raise HTTPException(404, "Active project not found")
        if not project["repo_path"]:
            raise HTTPException(409, "Register this project's repository path before collection")
        documents = read_rules(project["repo_path"])
        if service.digest(documents) != body.snapshot_sha256:
            raise HTTPException(409, "Local rules changed since preview; preview again")
        state = await service.project_revision(conn, project_id)
        if state["revision"] != body.expected_revision:
            raise HTTPException(409, "Project ADC changed; preview again")
        if state["release"] and state["release"]["id"] != body.release_id:
            raise HTTPException(409, "Collection preserves the pinned baseline; upgrade separately")
        baseline = await service.read_release(conn, body.release_id)
        changes = rule_changes(state, baseline["documents"], documents, body.reason)
        saved = await service.save_revision(conn, project_id, service.ProjectUpdate(
            release_id=body.release_id, expected_revision=body.expected_revision,
            reason=body.reason, changes=changes,
        ), actor)
        if any(saved["documents"].get(p) != c for p, c in documents.items()):
            raise HTTPException(409, "Collected rules differ from effective ADC; no revision committed")
        await conn.execute(
            """INSERT INTO adc_rule_collections
               (project_id,revision,source_path,snapshot_sha256,manifest_json)
               VALUES($1,$2,$3,$4,$5)""", project_id, saved["revision"], project["repo_path"],
            body.snapshot_sha256, service.canonical({
                p: hashlib.sha256(c.encode()).hexdigest() for p, c in documents.items()
            }),
        )
        return {"project_id": project_id, "revision": saved["revision"],
                "snapshot_sha256": body.snapshot_sha256, "document_count": len(documents)}


async def versions(conn):
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        releases = await service.list_releases(conn)
        projects = await conn.fetch(
            "SELECT id,project_name FROM projects WHERE is_active=1 ORDER BY project_name,id")
        revisions = await conn.fetch("""
            SELECT r.project_id,r.revision,r.release_id,r.actor,r.reason,r.created_at,r.restored_from,
                   c.snapshot_sha256
            FROM adc_project_revisions r JOIN projects p ON p.id=r.project_id AND p.is_active=1
            LEFT JOIN adc_rule_collections c ON c.project_id=r.project_id AND c.revision=r.revision
            ORDER BY r.project_id,r.revision""")
        return {"releases": releases, "projects": [dict(p) for p in projects],
                "revisions": [dict(r) for r in revisions]}
