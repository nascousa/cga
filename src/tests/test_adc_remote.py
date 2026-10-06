import asyncio
import hashlib
import json
import os
import socket
import unittest

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException

from backend.adc import service
from backend.adc.mcp import adc_call
from backend.adc.remote import OPERATIONS, RemoteQuery, query
from backend.adc.router import router
from backend.auth import pgshim
from backend.auth.context import _current_project_db_id, _current_project_external_id
from backend.auth.crystals import crystal_suite_headers
from backend.auth.database import _CREATE_TABLES, get_db
from backend.auth.dependencies import get_current_user
from backend.auth.middleware import ProjectTokenMiddleware
from backend.auth.security import hash_token
from backend.cga_relay.router import router as relay_router, account_router
from backend.tools.server import mcp


@unittest.skipUnless(os.environ.get("ADC_TEST_DSN"), "Requires isolated adc_test database")
class RemoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await asyncpg.connect(os.environ["ADC_TEST_DSN"])
        if await self.db.fetchval("SELECT current_database()") != "adc_test":
            await self.db.close()
            raise RuntimeError("Refusing a non-test database")
        await self.db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await self.db.execute(_CREATE_TABLES)
        await service.initialize(self.db)
        self.project = await self.db.fetchval("INSERT INTO projects(project_name,project_id) VALUES('one','project-one') RETURNING id")
        self.other = await self.db.fetchval("INSERT INTO projects(project_name,project_id) VALUES('two','project-two') RETURNING id")
        await service.onboarding(self.db, self.project, "admin")
        await service.onboarding(self.db, self.other, "admin")
        self.token = "adc-remote-isolated-test-token"
        await self.db.execute("INSERT INTO project_tokens(project_id,token_type,token_hash,token_hint) VALUES($1,'mcp',$2,'test')",
                              self.project, hash_token(self.token))
        self.pool = pgshim.PgPool(os.environ["ADC_TEST_DSN"])
        await self.pool.open()
        self.old_pool = pgshim.set_pool(self.pool)
        self.headers = {**crystal_suite_headers(), "Authorization":f"Bearer {self.token}", "X-Project-ID":"project-one"}
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api")
        self.app.include_router(relay_router, prefix="/api")
        self.app.include_router(account_router, prefix="/api")
        self.app.add_middleware(ProjectTokenMiddleware)

    async def asyncTearDown(self):
        pgshim.set_pool(self.old_pool)
        await self.pool.close()
        await self.db.close()

    async def test_all_read_operations_and_bundle_hashes(self):
        state = await service.project_revision(self.db, self.project)
        r = state["release"]["id"]
        requests = [RemoteQuery(operation="adc_catalog"), RemoteQuery(operation="adc_release", release_id=r),
                    RemoteQuery(operation="adc_current"), RemoteQuery(operation="adc_history"),
                    RemoteQuery(operation="adc_diff", release_id=r),
                    RemoteQuery(operation="adc_document", path=".adc/index.md"), RemoteQuery(operation="adc_bundle")]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            for request in requests:
                with self.subTest(operation=request.operation):
                    response = await client.post("/api/project/adc/query", json=request.model_dump(), headers=self.headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    payload = {"tool":request.operation,"project_id":"project-one","arguments":request.model_dump(exclude={"operation"}, exclude_none=True)}
                    relay = await client.post("/api/project/cga-relay/mcp-tool", json=payload, headers=self.headers)
                    self.assertEqual(relay.status_code, 200, relay.text)
                    self.assertTrue(relay.json()["ok"])
            bundle = (await client.post("/api/project/adc/query", json={"operation":"adc_bundle"}, headers=self.headers)).json()
        self.assertEqual(len(bundle["files"]), 45)
        self.assertEqual(bundle["project_id"], "project-one")
        for file in bundle["files"]:
            self.assertEqual(hashlib.sha256(file["content"].encode()).hexdigest(), file["sha256"])
        lock = json.loads(next(f["content"] for f in bundle["files"] if f["path"] == ".adc/adc-lock.json"))
        self.assertEqual(lock["revision"], 1)
        self.assertEqual(lock["project_external_id"], "project-one")

    async def test_mcp_discovery_and_bound_project_tools(self):
        tools = await mcp.list_tools()
        self.assertTrue(set(OPERATIONS).issubset({t.name for t in tools}))
        with self.assertRaises(ValueError):
            await adc_call("adc_current")
        bound = _current_project_db_id.set(self.project)
        external = _current_project_external_id.set("project-one")
        try:
            # Call through FastMCP's real tool manager, not only the helper.
            result = await mcp.call_tool("adc_document", {"path":".adc/index.md"})
            self.assertIn("sha256", str(result))
            current = await adc_call("adc_current")
            self.assertEqual(current["project_id"], self.project)
        finally:
            _current_project_db_id.reset(bound)
            _current_project_external_id.reset(external)

    async def test_authenticated_sse_transport_calls_every_adc_tool(self):
        import uvicorn
        from mcp import ClientSession
        from mcp.client.sse import sse_client
        self.app.mount("/mcp", mcp.sse_app())
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(self.app, log_level="error", lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(100):
                if server.started:
                    break
                if task.done():
                    await task
                    self.fail("SSE server exited before startup")
                await asyncio.sleep(.02)
            self.assertTrue(server.started)
            async with sse_client(f"http://127.0.0.1:{port}/mcp/sse", headers=self.headers) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertTrue(set(OPERATIONS).issubset({t.name for t in tools.tools}))
                    release = (await service.project_revision(self.db, self.project))["release_id"]
                    for name in OPERATIONS:
                        args = {"release_id":release} if name in ("adc_release", "adc_diff") else (
                            {"path":".adc/index.md"} if name == "adc_document" else {})
                        result = await session.call_tool(name, args)
                        self.assertFalse(result.isError, f"{name}: {result}")
            async with httpx.AsyncClient() as client:
                forbidden = await client.get(f"http://127.0.0.1:{port}/mcp/sse",
                    headers={**self.headers, "X-Project-ID":"project-two"})
                self.assertEqual(forbidden.status_code, 403)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)
            sock.close()

    async def test_cross_project_admin_mutations_and_malformed_arguments_denied(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            for payload in [
                {"tool":"adc_current","project_id":"project-two","arguments":{}},
                {"tool":"adc_current","project_id":"project-one","arguments":{"project_id":"project-two"}},
            ]:
                self.assertEqual((await client.post("/api/project/cga-relay/mcp-tool", json=payload, headers=self.headers)).status_code, 403)
            bad = {"tool":"adc_current","project_id":"project-one","arguments":{"project_db_id":self.other}}
            self.assertEqual((await client.post("/api/project/cga-relay/mcp-tool", json=bad, headers=self.headers)).status_code, 422)
            self.assertEqual((await client.post("/api/project/adc/query", json={"operation":"adc_publish"}, headers=self.headers)).status_code, 422)
            self.assertEqual((await client.post(f"/api/adc/projects/{self.project}/revisions",
                json={"release_id":1,"expected_revision":1,"reason":"Unauthorized"}, headers=self.headers)).status_code, 401)
            self.assertEqual((await client.post("/api/project/adc/query", json={"operation":"adc_document","path":"../secret"}, headers=self.headers)).status_code, 422)
            self.assertEqual((await client.post("/api/project/adc/query", json={"operation":"adc_current","revision":999}, headers=self.headers)).status_code, 404)

    async def test_account_bridge_respects_project_groups(self):
        self.app.dependency_overrides[get_current_user] = lambda: {"id":42,"username":"developer","role":"developer"}
        await self.db.execute("INSERT INTO user_groups(group_name) VALUES('restricted')")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            response = await client.post("/api/auth/cga-relay/mcp-tool",
                json={"tool":"adc_current","project_id":"project-one","arguments":{}}, headers=crystal_suite_headers())
            self.assertEqual(response.status_code, 403)
            self.app.dependency_overrides[get_current_user] = lambda: {"id":42,"username":"admin","role":"admin"}
            response = await client.post("/api/auth/cga-relay/mcp-tool",
                json={"tool":"adc_current","project_id":"project-one","arguments":{}}, headers=crystal_suite_headers())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["result"]["project_external_id"], "project-one")

    async def test_unbound_project_cannot_download_an_unapproved_latest(self):
        project = await self.db.fetchval("INSERT INTO projects(project_name,project_id) VALUES('unbound','unbound') RETURNING id")
        with self.assertRaises(HTTPException) as failure:
            await query(self.db, project, RemoteQuery(operation="adc_bundle"))
        self.assertEqual(failure.exception.status_code, 409)

    async def test_unicode_history_pagination_and_response_limit(self):
        from unittest.mock import AsyncMock, patch
        release = await service.publish(self.db, service.ReleaseCreate(
            version="2.0.0", documents={".adc/index.md":"Unicode: \u4e2d\u6587 \U0001f600\n"},
            reason="Remote Unicode verification"), "admin")
        await service.save_revision(self.db, self.project, service.ProjectUpdate(
            release_id=release["id"], expected_revision=1, reason="Approved upgrade"), "admin")
        doc = await query(self.db, self.project, RemoteQuery(operation="adc_document", path=".adc/index.md"))
        self.assertEqual(doc["content"], "Unicode: \u4e2d\u6587 \U0001f600\n")
        page = await query(self.db, self.project, RemoteQuery(operation="adc_history", offset=1, limit=1))
        self.assertEqual(page["total"], 2)
        self.assertEqual([row["revision"] for row in page["history"]], [1])
        old = await query(self.db, self.project, RemoteQuery(operation="adc_bundle", revision=1))
        self.assertTrue(old["historical"])
        self.assertEqual(old["version"], "1.1.23")
        with patch("backend.adc.remote._query", AsyncMock(return_value={"data":"x" * (7 * 1024 * 1024)})):
            with self.assertRaises(HTTPException) as error:
                await query(self.db, self.project, RemoteQuery(operation="adc_bundle"))
            self.assertEqual(error.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()
