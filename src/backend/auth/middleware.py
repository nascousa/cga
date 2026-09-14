"""Pure-ASGI middleware: validate Bearer project tokens on /mcp and /api/project paths.

Uses a pure ASGI class (not BaseHTTPMiddleware) so that ContextVar values
set here propagate correctly into route handlers and MCP tool functions.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import HTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.auth.access import registered_project_scope
from backend.auth.context import bind_project_scope
from backend.auth import pgshim
from backend.auth.crystals import validate_crystal_suite_headers
from backend.auth.security import hash_token

if TYPE_CHECKING:
    pass

_PROTECTED_ROUTE_RULES = (
    {
        "prefix": "/mcp",
        "allowed_token_types": frozenset({"mcp"}),
        "require_project_id": True,
        "endpoint_label": "MCP endpoint",
    },
    {
        "prefix": "/api/project",
        "allowed_token_types": frozenset({"mcp"}),
        "require_project_id": False,
        "endpoint_label": "project endpoint",
    },
)

_PUBLIC_MCP_DISCOVERY_PATHS = frozenset({"/mcp", "/mcp/"})


def _is_public_mcp_discovery(path: str, method: str) -> bool:
    return method in {"GET", "HEAD", "OPTIONS"} and path in _PUBLIC_MCP_DISCOVERY_PATHS


def _match_route_rule(path: str) -> dict | None:
    for rule in _PROTECTED_ROUTE_RULES:
        if path.startswith(rule["prefix"]):
            return rule
    return None


async def _send_error(send: Send, body: dict, status: int) -> None:
    data = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(data)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": data, "more_body": False})


class ProjectTokenMiddleware:
    """Pure ASGI middleware that validates project Bearer tokens by route policy."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        if _is_public_mcp_discovery(path, method):
            await self.app(scope, receive, send)
            return

        route_rule = _match_route_rule(path)
        if route_rule is None:
            await self.app(scope, receive, send)
            return

        # Parse headers from ASGI scope
        raw_headers: dict[bytes, bytes] = {}
        for name, value in scope.get("headers", []):
            raw_headers[name.lower()] = value

        if crystal_error := validate_crystal_suite_headers(raw_headers):
            await _send_error(send, {"detail": crystal_error}, 426)
            return

        auth = raw_headers.get(b"authorization", b"").decode()
        if not auth.startswith("Bearer "):
            await _send_error(send, {"detail": "Missing Bearer token"}, 401)
            return

        project_id_raw = raw_headers.get(b"x-project-id", b"").decode()
        if not project_id_raw:
            qs = scope.get("query_string", b"").decode()
            params = {
                k: v
                for part in qs.split("&")
                if "=" in part
                for k, v in [part.split("=", 1)]
            }
            project_id_raw = params.get("project_id", "")

        project_id = project_id_raw.strip()
        if not project_id and route_rule["require_project_id"]:
            await _send_error(
                send, {"detail": "Missing project_id (use X-Project-ID header)"}, 401
            )
            return

        token = auth[len("Bearer "):]

        # Validate token against DB
        try:
            digest = hash_token(token)
            async with pgshim.get_pool().acquire() as db:
                async with db.execute(
                    """
                    SELECT pt.id, pt.project_id, pt.token_type,
                           p.project_id AS project_external_id,
                           p.project_name, p.repo_path
                    FROM project_tokens pt
                    JOIN projects p ON p.id = pt.project_id
                    WHERE pt.token_hash = ? AND pt.is_active = 1 AND p.is_active = 1
                    """,
                    (digest,),
                ) as cur:
                    row = await cur.fetchone()
        except Exception:
            row = None

        if not row:
            await _send_error(send, {"detail": "Invalid token"}, 401)
            return

        if row["token_type"] not in route_rule["allowed_token_types"]:
            await _send_error(
                send,
                {"detail": f"Token type not allowed for {route_rule['endpoint_label']}"},
                403,
            )
            return

        effective_project_id = project_id or str(row["project_external_id"])

        if row["project_external_id"] != effective_project_id:
            await _send_error(
                send, {"detail": "Token is not valid for this project_id"}, 403
            )
            return

        try:
            project_scope = registered_project_scope({
                "id": row["project_id"],
                "project_id": row["project_external_id"],
                "project_name": row["project_name"],
                "repo_path": row["repo_path"],
            })
        except HTTPException as exc:
            await _send_error(send, {"detail": exc.detail}, exc.status_code)
            return

        # Store auth context in ASGI scope state (readable via request.state)
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["project_id"] = effective_project_id
        scope["state"]["project_db_id"] = row["project_id"]
        scope["state"]["project_name"] = row["project_name"]
        scope["state"]["project_token_id"] = row["id"]
        scope["state"]["project_token_type"] = row["token_type"]
        scope["state"]["registered_project_scope"] = project_scope

        # Set ContextVar so MCP tool functions pick up the right project graph.
        # Pure ASGI middleware propagates ContextVars correctly (unlike BaseHTTPMiddleware).
        with bind_project_scope(project_scope):
            await self.app(scope, receive, send)
