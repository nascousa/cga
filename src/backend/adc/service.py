from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import asyncpg
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA = """
CREATE TABLE IF NOT EXISTS adc_releases (
    id BIGSERIAL PRIMARY KEY,
    version TEXT UNIQUE NOT NULL,
    major INTEGER NOT NULL, minor INTEGER NOT NULL, patch INTEGER NOT NULL,
    documents_json TEXT NOT NULL, sha256 TEXT NOT NULL,
    reason TEXT NOT NULL, source TEXT NOT NULL,
    actor TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS adc_project_revisions (
    project_id BIGINT NOT NULL REFERENCES projects(id),
    revision INTEGER NOT NULL,
    release_id BIGINT NOT NULL REFERENCES adc_releases(id),
    changes_json TEXT NOT NULL, reviewed_paths_json TEXT NOT NULL,
    reason TEXT NOT NULL, actor TEXT NOT NULL,
    restored_from INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(project_id, revision)
);
CREATE OR REPLACE FUNCTION adc_reject_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'ADC history is immutable; append a new release or revision'; END;
$$ LANGUAGE plpgsql;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='adc_releases_immutable' AND tgrelid='adc_releases'::regclass) THEN
        CREATE TRIGGER adc_releases_immutable BEFORE UPDATE OR DELETE ON adc_releases
        FOR EACH ROW EXECUTE FUNCTION adc_reject_mutation();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='adc_revisions_immutable' AND tgrelid='adc_project_revisions'::regclass) THEN
        CREATE TRIGGER adc_revisions_immutable BEFORE UPDATE OR DELETE ON adc_project_revisions
        FOR EACH ROW EXECUTE FUNCTION adc_reject_mutation();
    END IF;
END $$;
"""


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def document_path(value: str) -> str:
    parts = value.split("/")
    if (len(value) > 240 or len(parts) < 2 or parts[0] not in (".adc", ".github")
            or value.casefold() == ".adc/adc-lock.json"
            or any(not part or part in (".", "..") or part.endswith((".", " "))
                   or re.search(r'[\\:\x00-\x1f<>\"|?*]', part)
                   or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
                   for part in parts)):
        raise ValueError("Use a safe relative .adc/ or .github/ document path; adc-lock.json is reserved")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReleaseCreate(StrictModel):
    version: str = Field(pattern=r"^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})$")
    documents: dict[str, str]
    reason: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="CGA administrator", max_length=1000)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("A reason is required")
        return value.strip()

    @field_validator("documents")
    @classmethod
    def validate_documents(cls, value):
        if not value or len(value) > 500:
            raise ValueError("A release requires 1 to 500 documents")
        seen = set()
        for path, content in value.items():
            document_path(path)
            if path.casefold() in seen:
                raise ValueError("Document paths must be unique ignoring case")
            seen.add(path.casefold())
            if len(content.encode()) > 512_000:
                raise ValueError("A document cannot exceed 512 KB")
        if len(canonical(value).encode()) > 5_000_000:
            raise ValueError("A release cannot exceed 5 MB")
        return value


class Change(StrictModel):
    path: str
    kind: Literal["amendment", "override", "exemption"]
    content: str | None = Field(default=None, max_length=512_000)
    reason: str = Field(min_length=1, max_length=4000)
    expires_at: datetime | None = None

    _path = field_validator("path")(document_path)
    _reason = field_validator("reason")(ReleaseCreate.nonblank.__func__)

    @model_validator(mode="after")
    def validate_change(self):
        if self.kind == "exemption" and self.content is not None:
            raise ValueError("Exemptions contain a reason, not replacement content")
        if self.kind != "exemption" and self.content is None:
            raise ValueError("Amendments and overrides require content")
        if self.expires_at and (self.kind != "exemption" or self.expires_at.tzinfo is None):
            raise ValueError("Only exemptions support an expiry, with an explicit timezone")
        return self


class ProjectUpdate(StrictModel):
    release_id: int = Field(gt=0)
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=4000)
    changes: list[Change] = Field(default_factory=list, max_length=500)
    reviewed_paths: list[str] = Field(default_factory=list, max_length=500)

    _reason = field_validator("reason")(ReleaseCreate.nonblank.__func__)

    @model_validator(mode="after")
    def unique_changes(self):
        if len({c.path.casefold() for c in self.changes}) != len(self.changes):
            raise ValueError("Only one change per document is allowed")
        if len(canonical([c.model_dump(mode="json") for c in self.changes]).encode()) > 5_000_000:
            raise ValueError("Project changes cannot exceed 5 MB")
        return self


def effective(documents: dict[str, str], changes: list[Change], at: datetime | None = None):
    result = dict(documents)
    status = []
    now = at or datetime.now(timezone.utc)
    for change in changes:
        active = not change.expires_at or change.expires_at > now
        status.append({**change.model_dump(mode="json"), "active": active})
        if not active:
            continue
        if change.kind == "exemption":
            result.pop(change.path, None)
        else:
            result[change.path] = change.content
    return result, status


def release_record(row):
    result = dict(row)
    result["documents"] = json.loads(result.pop("documents_json"))
    return result


async def initialize(conn: asyncpg.Connection, *, seed: bool = True):
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(74123901)")
        await conn.execute(SCHEMA)
        if seed:
            seed_path = Path(__file__).with_name("seed.json")
            body = ReleaseCreate.model_validate_json(seed_path.read_text(encoding="utf-8"))
            existing = await conn.fetchrow("SELECT sha256 FROM adc_releases WHERE version=$1", body.version)
            if existing is None:
                await publish(conn, body, "system:local-adc-import")
            elif existing["sha256"] != digest(body.documents):
                raise RuntimeError("Bundled ADC release differs from its immutable published version")


async def publish(conn, body: ReleaseCreate, actor: str):
    major, minor, patch = map(int, body.version.split("."))
    row = await conn.fetchrow(
        """INSERT INTO adc_releases(version,major,minor,patch,documents_json,sha256,reason,source,actor)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(version) DO NOTHING RETURNING *""",
        body.version, major, minor, patch, canonical(body.documents), digest(body.documents), body.reason, body.source, actor,
    )
    if row is None:
        raise HTTPException(409, "This version already exists and cannot be changed")
    return release_record(row)


async def read_release(conn, release_id: int):
    row = await conn.fetchrow("SELECT * FROM adc_releases WHERE id=$1", release_id)
    if row is None:
        raise HTTPException(404, "ADC release not found")
    return release_record(row)


async def list_releases(conn):
    rows = await conn.fetch("""SELECT id,version,sha256,reason,source,actor,created_at
        FROM adc_releases ORDER BY major DESC,minor DESC,patch DESC""")
    return [dict(row) for row in rows]


async def project_revision(conn, project_id: int, revision: int | None = None):
    row = await conn.fetchrow("""SELECT * FROM adc_project_revisions
        WHERE project_id=$1 AND ($2::integer IS NULL OR revision=$2)
        ORDER BY revision DESC LIMIT 1""", project_id, revision)
    if row is None:
        if revision is not None:
            raise HTTPException(404, "ADC project revision not found")
        return {"project_id": project_id, "revision": 0, "release": None, "changes": [], "documents": {}}
    state = dict(row)
    release = await read_release(conn, row["release_id"])
    changes = [Change.model_validate(c) for c in json.loads(state.pop("changes_json"))]
    evaluated_at = row["created_at"] if revision is not None else datetime.now(timezone.utc)
    documents, statuses = effective(release["documents"], changes, evaluated_at)
    state.update(release=release, changes=statuses, documents=documents, effective_sha256=digest(documents),
                 evaluated_at=evaluated_at, reviewed_paths=json.loads(state.pop("reviewed_paths_json")))
    return state


async def save_revision(conn, project_id: int, body: ProjectUpdate, actor: str, restored_from: int | None = None):
    async with conn.transaction():
        project = await conn.fetchrow("SELECT id FROM projects WHERE id=$1 AND is_active=1 FOR UPDATE", project_id)
        if project is None:
            raise HTTPException(404, "Active project not found")
        old = await project_revision(conn, project_id)
        if old["revision"] != body.expected_revision:
            raise HTTPException(409, "Project ADC changed; reload before saving")
        release = await read_release(conn, body.release_id)
        base = release["documents"]
        for change in body.changes:
            if (change.kind == "amendment") == (change.path in base):
                raise HTTPException(422, f"{change.kind} has an invalid baseline target: {change.path}")
            if any(p.casefold() == change.path.casefold() and p != change.path for p in base):
                raise HTTPException(422, f"Path differs from baseline only by case: {change.path}")
        if old["release"] and old["release"]["id"] != body.release_id:
            previous = old["release"]["documents"]
            affected = {c["path"] for c in old["changes"]} | {c.path for c in body.changes}
            conflicts = sorted(p for p in affected if previous.get(p) != base.get(p) and p not in body.reviewed_paths)
            if conflicts:
                raise HTTPException(409, "Review changed baseline documents before upgrading: " + ", ".join(conflicts))
        await conn.execute("""INSERT INTO adc_project_revisions
            (project_id,revision,release_id,changes_json,reviewed_paths_json,reason,actor,restored_from)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8)""",
            project_id, old["revision"] + 1, body.release_id,
            canonical([c.model_dump(mode="json") for c in body.changes]),
            canonical(body.reviewed_paths), body.reason, actor, restored_from,
        )
        return await project_revision(conn, project_id)


async def onboarding(conn, project_id: int, actor: str):
    if await conn.fetchval("SELECT 1 FROM adc_project_revisions WHERE project_id=$1", project_id):
        return
    release = await conn.fetchval("SELECT id FROM adc_releases ORDER BY major DESC,minor DESC,patch DESC LIMIT 1")
    if release is None:
        raise HTTPException(409, "Publish an ADC release before onboarding a project")
    await save_revision(conn, project_id, ProjectUpdate(
        release_id=release, expected_revision=0, reason="Onboarding: pinned latest published ADC"), actor)


def archive(state: dict) -> bytes:
    output = io.BytesIO()
    metadata = {k: v for k, v in state.items() if k != "documents"}
    if metadata.get("release"):
        metadata["release"] = {k: v for k, v in metadata["release"].items() if k != "documents"}
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in sorted(state["documents"].items()):
            document_path(path)
            z.writestr(path, content)
        z.writestr(".adc/adc-lock.json", json.dumps(metadata, ensure_ascii=False, indent=2, default=str))
    return output.getvalue()
