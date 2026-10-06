import io
import json
import os
import unittest
import zipfile
import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from backend.adc.service import (
    Change, ProjectUpdate, ReleaseCreate, archive, effective, initialize,
    onboarding, project_revision, publish, read_release, save_revision,
)
from backend.adc.router import router
from backend.auth.database import get_db
from backend.auth.dependencies import get_current_user
from backend.auth.pgshim import Connection


DOCS = {".adc/index.md": "# ADC", ".adc/standards/testing.md": "Test every change."}


class ModelTests(unittest.TestCase):
    def test_paths_and_case_collisions(self):
        for path in ("../secret", "/absolute", ".adc/../x", ".adc\\x", ".adc//x", ".adc/CON", ".adc/adc-lock.json"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                ReleaseCreate(version="1.0.0", documents={path: "x"}, reason="Initial")
        with self.assertRaises(ValidationError):
            ReleaseCreate(version="1.0.0", documents={".adc/A.md": "a", ".adc/a.md": "b"}, reason="Initial")

    def test_expired_exemption_restores_baseline(self):
        expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        changes = [Change(path=".adc/standards/testing.md", kind="exemption", reason="Temporary", expires_at=expired)]
        docs, status = effective(DOCS, changes)
        self.assertEqual(docs, DOCS)
        self.assertFalse(status[0]["active"])
        active = [Change(path=".adc/standards/testing.md", kind="exemption", reason="Reviewed exception")]
        docs, _ = effective(DOCS, active)
        self.assertNotIn(".adc/standards/testing.md", docs)

    def test_amendment_override_and_archive_trace(self):
        changes = [
            Change(path=".adc/index.md", kind="override", content="Project rules", reason="Project requirements"),
            Change(path=".adc/knowledge/project.md", kind="amendment", content="Project context", reason="Local knowledge"),
        ]
        docs, _ = effective(DOCS, changes)
        self.assertEqual(docs[".adc/index.md"], "Project rules")
        payload = {"documents": docs, "release": {"version": "1.0.0"}, "changes": [c.model_dump(mode="json") for c in changes]}
        with zipfile.ZipFile(io.BytesIO(archive(payload))) as z:
            self.assertEqual(z.read(".adc/index.md").decode(), "Project rules")
            self.assertIn(".adc/adc-lock.json", z.namelist())
            self.assertEqual(json.loads(z.read(".adc/adc-lock.json"))["release"]["version"], "1.0.0")


@unittest.skipUnless(os.environ.get("ADC_TEST_DSN"), "Set ADC_TEST_DSN to an isolated PostgreSQL database")
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await asyncpg.connect(os.environ["ADC_TEST_DSN"])
        if await self.db.fetchval("SELECT current_database()") != "adc_test":
            await self.db.close()
            raise RuntimeError("ADC destructive integration fixture requires database named adc_test")
        await self.db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await self.db.execute("CREATE TABLE projects(id BIGSERIAL PRIMARY KEY, is_active INTEGER NOT NULL DEFAULT 1)")
        await self.db.execute("""
            CREATE TABLE user_groups(id BIGINT PRIMARY KEY, is_active INTEGER);
            CREATE TABLE user_group_members(user_id BIGINT, group_id BIGINT);
            CREATE TABLE project_group_access(group_id BIGINT, project_id BIGINT);
        """)
        await initialize(self.db, seed=False)
        self.project = await self.db.fetchval("INSERT INTO projects DEFAULT VALUES RETURNING id")
        self.r1 = await publish(self.db, ReleaseCreate(version="1.0.0", documents=DOCS, reason="Initial"), "admin")

    async def asyncTearDown(self):
        await self.db.close()

    async def test_immutable_release_and_semantic_latest_onboarding(self):
        with self.assertRaises(HTTPException) as cm:
            await publish(self.db, ReleaseCreate(version="1.0.0", documents=DOCS, reason="Replace"), "admin")
        self.assertEqual(cm.exception.status_code, 409)
        await publish(self.db, ReleaseCreate(version="1.10.0", documents=DOCS, reason="New"), "admin")
        await publish(self.db, ReleaseCreate(version="1.2.0", documents=DOCS, reason="Historical import"), "admin")
        await onboarding(self.db, self.project, "admin")
        state = await project_revision(self.db, self.project)
        self.assertEqual(state["release"]["version"], "1.10.0")
        with self.assertRaises(asyncpg.exceptions.RaiseError):
            await self.db.execute("UPDATE adc_releases SET reason='tampered' WHERE id=$1", self.r1["id"])
        self.assertEqual((await read_release(self.db, self.r1["id"]))["documents"], DOCS)

    async def test_revision_conflict_upgrade_review_rollback_and_isolation(self):
        first = ProjectUpdate(release_id=self.r1["id"], expected_revision=0, reason="Adopt",
                              changes=[Change(path=".adc/index.md", kind="override", content="Local", reason="Local rule")])
        s1 = await save_revision(self.db, self.project, first, "admin")
        with self.assertRaises(HTTPException) as stale:
            await save_revision(self.db, self.project, first, "admin")
        self.assertEqual(stale.exception.status_code, 409)
        changed = {**DOCS, ".adc/index.md": "Updated baseline"}
        r2 = await publish(self.db, ReleaseCreate(version="2.0.0", documents=changed, reason="Next"), "admin")
        update = ProjectUpdate(release_id=r2["id"], expected_revision=1, reason="Upgrade", changes=first.changes)
        with self.assertRaises(HTTPException) as conflict:
            await save_revision(self.db, self.project, update, "admin")
        self.assertEqual(conflict.exception.status_code, 409)
        update.reviewed_paths = [".adc/index.md"]
        s2 = await save_revision(self.db, self.project, update, "admin")
        self.assertEqual(s2["revision"], 2)
        self.assertEqual(s2["documents"][".adc/index.md"], "Local")
        self.assertEqual((await project_revision(self.db, self.project, 1))["release"]["version"], "1.0.0")
        other = await self.db.fetchval("INSERT INTO projects DEFAULT VALUES RETURNING id")
        await onboarding(self.db, other, "admin")
        self.assertEqual((await project_revision(self.db, other))["documents"][".adc/index.md"], "Updated baseline")
        rollback = ProjectUpdate(release_id=s1["release"]["id"], expected_revision=2, reason="Rollback",
                                 changes=first.changes, reviewed_paths=[".adc/index.md"])
        self.assertEqual((await save_revision(self.db, self.project, rollback, "admin", restored_from=1))["revision"], 3)
        self.assertEqual(await self.db.fetchval("SELECT count(*) FROM adc_project_revisions WHERE project_id=$1", self.project), 3)

    async def test_invalid_overlay_and_atomic_onboarding(self):
        bad = ProjectUpdate(release_id=self.r1["id"], expected_revision=0, reason="Adopt",
                            changes=[Change(path=".adc/missing.md", kind="override", content="x", reason="Wrong target")])
        with self.assertRaises(HTTPException):
            await save_revision(self.db, self.project, bad, "admin")
        self.assertEqual(await self.db.fetchval("SELECT count(*) FROM adc_project_revisions"), 0)
        async with self.db.transaction():
            await onboarding(self.db, self.project, "admin")
        state = await project_revision(self.db, self.project)
        self.assertEqual(state["revision"], 1)
        await onboarding(self.db, self.project, "admin")
        self.assertEqual(await self.db.fetchval("SELECT count(*) FROM adc_project_revisions"), 1)

    async def test_concurrent_writes_have_one_winner(self):
        other = await asyncpg.connect(os.environ["ADC_TEST_DSN"])
        body = ProjectUpdate(release_id=self.r1["id"], expected_revision=0, reason="Concurrent adoption")
        try:
            results = await asyncio.gather(
                save_revision(self.db, self.project, body, "admin"),
                save_revision(other, self.project, body, "admin"), return_exceptions=True)
        finally:
            await other.close()
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        errors = [r for r in results if isinstance(r, HTTPException)]
        self.assertEqual([e.status_code for e in errors], [409])

    async def test_bundled_seed_and_real_project_creation(self):
        from backend.auth.database import _CREATE_TABLES
        from backend.auth.models import ProjectCreate
        from backend.auth.router import create_project
        await self.db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await self.db.execute(_CREATE_TABLES)
        await initialize(self.db)
        seed = await self.db.fetchrow("SELECT * FROM adc_releases WHERE version='1.1.23'")
        self.assertEqual(len(json.loads(seed["documents_json"])), 44)
        await initialize(self.db)
        self.assertEqual(await self.db.fetchval("SELECT count(*) FROM adc_releases"), 1)
        project = await create_project(ProjectCreate(project_name="adc-onboarding-test"), {"username":"admin"}, Connection(self.db))
        state = await project_revision(self.db, project.id)
        self.assertEqual(state["release"]["version"], "1.1.23")
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["actor"], "admin")
        with self.assertRaises(HTTPException) as duplicate:
            await create_project(ProjectCreate(project_name="adc-onboarding-test"), {"username":"admin"}, Connection(self.db))
        self.assertEqual(duplicate.exception.status_code, 409)
        self.assertEqual(await self.db.fetchval("SELECT count(*) FROM adc_project_revisions"), 1)

    async def test_http_authorization_download_and_revision_restore(self):
        app = FastAPI()
        app.include_router(router, prefix="/api")

        async def database():
            yield Connection(self.db)

        app.dependency_overrides[get_db] = database
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            self.assertEqual((await client.get("/api/adc/releases")).status_code, 401)
            app.dependency_overrides[get_current_user] = lambda: {"id": 7, "username": "dev", "role": "developer"}
            self.assertEqual((await client.post("/api/adc/releases", json={"version":"3.0.0","documents":DOCS,"reason":"Test"})).status_code, 403)
            await self.db.execute("INSERT INTO user_groups VALUES(1,1)")
            self.assertEqual((await client.get(f"/api/adc/projects/{self.project}")).status_code, 403)
            self.assertEqual((await client.get(f"/api/adc/projects/{self.project}/download")).status_code, 403)
            self.assertEqual((await client.get("/api/project/adc")).status_code, 401)
            app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "admin", "role": "admin"}
            body = {"release_id":self.r1["id"],"expected_revision":0,"reason":"Adopt"}
            self.assertEqual((await client.post(f"/api/adc/projects/{self.project}/revisions", json=body)).status_code, 201)
            response = await client.get(f"/api/adc/projects/{self.project}/download")
            self.assertEqual(response.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                self.assertEqual(z.read(".adc/index.md").decode(), DOCS[".adc/index.md"])
            response = await client.post(f"/api/adc/projects/{self.project}/restore", json={"revision":1,"expected_revision":1,"reason":"Restore"})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["restored_from"], 1)
            self.assertEqual(response.json()["revision"], 2)
            self.assertEqual((await client.get(f"/api/adc/projects/{self.project}?revision=999")).status_code, 404)
            self.assertEqual((await client.post(f"/api/adc/projects/{self.project}/restore",
                                               json={"revision":1,"expected_revision":2,"reason":" "})).status_code, 422)

    async def test_project_token_reads_only_its_own_pinned_release(self):
        from backend.auth import pgshim
        from backend.auth.crystals import crystal_suite_headers
        from backend.auth.database import _CREATE_TABLES
        from backend.auth.middleware import ProjectTokenMiddleware
        from backend.auth.security import hash_token
        await self.db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await self.db.execute(_CREATE_TABLES)
        await initialize(self.db)
        project = await self.db.fetchval("INSERT INTO projects(project_name,project_id) VALUES('one','one-id') RETURNING id")
        await onboarding(self.db, project, "admin")
        token = "isolated-test-project-token"
        await self.db.execute("""INSERT INTO project_tokens(project_id,token_type,token_hash,token_hint)
            VALUES($1,'mcp',$2,'test')""", project, hash_token(token))
        pool = pgshim.PgPool(os.environ["ADC_TEST_DSN"])
        await pool.open()
        previous = pgshim.set_pool(pool)
        try:
            app = FastAPI()
            app.include_router(router, prefix="/api")
            app.add_middleware(ProjectTokenMiddleware)
            headers = {**crystal_suite_headers(), "Authorization":f"Bearer {token}", "X-Project-ID":"one-id"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/project/adc", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["project_id"], project)
                self.assertEqual(response.json()["release"]["version"], "1.1.23")
                self.assertEqual((await client.get("/api/project/adc/download", headers=headers)).status_code, 200)
                headers["X-Project-ID"] = "another-project"
                self.assertEqual((await client.get("/api/project/adc", headers=headers)).status_code, 403)
                self.assertEqual((await client.post("/api/adc/releases", headers=headers,
                    json={"version":"4.0.0","documents":DOCS,"reason":"Forbidden"})).status_code, 401)
        finally:
            pgshim.set_pool(previous)
            await pool.close()


if __name__ == "__main__":
    unittest.main()
