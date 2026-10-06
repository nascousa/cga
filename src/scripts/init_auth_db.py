#!/usr/bin/env python3
"""
init_auth_db.py
---------------
Bootstrap the auth PostgreSQL database (asyncpg via the pgshim):
  1. Create tables (idempotent).
  2. Create the initial admin user (ADMIN_USERNAME / ADMIN_PASSWORD env vars,
     defaults: admin / changeme).
  3. Optionally import projects + tokens from config/project-token-registry.json
      when a caller explicitly supplies that file (skips entries that already exist).

The Docker Desktop release package does not ship Nate Scott's local projects,
prebuilt indexes, database dumps, or sample/demo project data.

Safe to re-run; project + token inserts use ``ON CONFLICT DO NOTHING``.

Usage:
    python -m src.scripts.init_auth_db
    # or inside Docker:
    python /app/src/scripts/init_auth_db.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Allow running as a script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.auth.database import DB_PATH, init_db
from backend.auth.pgshim import get_pool
from backend.auth.security import hash_password, hash_token, token_hint

REGISTRY_PATH = os.getenv(
    "TOKEN_REGISTRY_PATH",
    str(Path(__file__).resolve().parents[2] / "config" / "project-token-registry.json"),
)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")


async def main() -> None:
    print(f"[init_auth_db] PG DSN: {DB_PATH}")
    await init_db()
    print("[init_auth_db] Tables ensured.")

    async with get_pool().acquire() as db:
        # ── Create admin user ──────────────────────────────────────────────
        async with db.execute(
            "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
        ) as cur:
            existing = await cur.fetchone()

        if not existing:
            hashed = hash_password(ADMIN_PASSWORD)
            await db.execute(
                "INSERT INTO users(username, password_hash, role) VALUES(?,?,?)",
                (ADMIN_USERNAME, hashed, "admin"),
            )
            print(f"[init_auth_db] Admin user '{ADMIN_USERNAME}' created.")
        else:
            print(f"[init_auth_db] Admin user '{ADMIN_USERNAME}' already exists, skipping.")

        # ── Import registry ────────────────────────────────────────────────
        registry_file = Path(REGISTRY_PATH)
        if not registry_file.exists():
            print(f"[init_auth_db] Registry not found at {REGISTRY_PATH}, skipping import.")
            return

        registry = json.loads(registry_file.read_text(encoding="utf-8"))
        projects = registry.get("projects", [])
        print(f"[init_auth_db] Importing {len(projects)} project(s) from registry.")

        for proj in projects:
            project_name = proj.get("projectName", proj.get("projectKey", ""))
            project_id = proj["projectId"]
            upstream_url = proj.get("upstreamUrl", "")
            description = proj.get("description", "")

            # Upsert project (ON CONFLICT DO NOTHING keeps existing rows)
            async with db.raw.transaction():
                async with db.execute(
                    """INSERT INTO projects(project_name, project_id, upstream_url, description)
                       VALUES(?,?,?,?)
                       ON CONFLICT (project_name) DO NOTHING RETURNING id""",
                    (project_name, project_id, upstream_url, description),
                ) as cur:
                    created = await cur.fetchone()
                if created:
                    from backend.adc.service import onboarding
                    await onboarding(db.raw, created["id"], "system:registry-import")

            # Fetch row id
            async with db.execute(
                "SELECT id FROM projects WHERE project_name = ?", (project_name,)
            ) as cur:
                row = await cur.fetchone()
            if not row:
                print(f"[init_auth_db]   {project_name}: missing after upsert, skipping tokens.")
                continue
            db_project_id = row["id"]

            # Import mcp token
            mcp_raw = proj.get("mcp", {}).get("token", "")
            if mcp_raw:
                await db.execute(
                    """INSERT INTO project_tokens(project_id, token_type, token_hash, token_hint)
                       VALUES(?,?,?,?)
                       ON CONFLICT (token_hash) DO NOTHING""",
                    (db_project_id, "mcp", hash_token(mcp_raw), token_hint(mcp_raw)),
                )
                print(f"[init_auth_db]   {project_name}: mcp token imported.")

        print("[init_auth_db] Done.")


if __name__ == "__main__":
    asyncio.run(main())
