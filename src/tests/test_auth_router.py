from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.auth import pgshim
from backend import runtime_config
from backend.auth import router as auth_router


async def _seed_project(
    db: pgshim.Connection,
    *,
    project_id: int = 1,
    project_name: str = "osagent",
    project_external_id: str = "OESIJQWHXY",
    repo_path: str | None = None,
) -> None:
    resolved_repo_path = repo_path or f"D:/Repos/{project_name}"
    await db.execute(
        "INSERT INTO projects(id, project_name, project_id, repo_path, is_active) "
        "VALUES (?, ?, ?, ?, 1)",
        (project_id, project_name, project_external_id, resolved_repo_path),
    )


async def _seed_token(
    db: pgshim.Connection,
    *,
    token_id: int,
    project_id: int,
    token_type: str,
    token_hash: str,
    token_hint: str,
    version: int = 1,
) -> None:
    await db.execute(
        "INSERT INTO project_tokens(id, project_id, token_type, token_hash, "
        "token_hint, version, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
        (token_id, project_id, token_type, token_hash, token_hint, version),
    )


def test_candidate_repo_paths_prefers_case_insensitive_local_repo_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_router, "_LOCAL_REPOS_ROOT", tmp_path)
    (tmp_path / "OSAgent").mkdir()

    candidates = auth_router._candidate_repo_paths("osagent")

    assert candidates[0] == str(tmp_path / "OSAgent")


def test_candidate_repo_paths_matches_punctuation_insensitive_local_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_router, "_LOCAL_REPOS_ROOT", tmp_path)
    (tmp_path / "ClaudeCLI").mkdir()
    (tmp_path / "HermesAgent").mkdir()

    claude_candidates = auth_router._candidate_repo_paths("claude-cli")
    hermes_candidates = auth_router._candidate_repo_paths("hermes-agent")

    assert claude_candidates[0] == str(tmp_path / "ClaudeCLI")
    assert hermes_candidates[0] == str(tmp_path / "HermesAgent")


def test_candidate_repo_paths_uses_configured_indexing_repos_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_root = tmp_path / "configured-repos"
    configured_root.mkdir()
    (configured_root / "BrowserAgent").mkdir()

    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", tmp_path / "runtime-config.json")
    monkeypatch.setattr(auth_router, "_LOCAL_REPOS_ROOT", tmp_path / "unused-local-root")
    runtime_config.update_runtime_config({"indexing": {"repos_root": str(configured_root)}})

    candidates = auth_router._candidate_repo_paths("browser-agent")

    assert candidates[0] == str(configured_root / "BrowserAgent")


def test_build_index_job_status_marks_stale_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_router, "INDEX_STALE_AFTER_SEC", 60)
    old_update = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()

    status = auth_router._build_index_job_status(
        {
            "job_id": "job-stale",
            "job_type": "index_full",
            "repo_path": "D:/Repos/OSAgent",
            "status": "processing",
            "created_at": old_update,
            "updated_at": old_update,
        },
        pending_by_id={},
        avg_duration_sec=30,
        processing_remaining=0,
    )

    assert status.status == "stale"
    assert status.is_stale is True
    assert status.age_seconds is not None
    assert status.age_seconds >= 60


@pytest.mark.asyncio
async def test_generate_project_token_uses_type_prefixes(auth_pg_pool) -> None:
    async with auth_pg_pool.acquire() as db:
        await _seed_project(db)

        mcp = await auth_router.generate_project_token(
            1,
            auth_router.GenerateTokenRequest(token_type="mcp"),
            _={"role": "admin"},
            db=db,
        )
        edge = await auth_router.generate_project_token(
            1,
            auth_router.GenerateTokenRequest(token_type="edge_agent"),
            _={"role": "admin"},
            db=db,
        )

    assert mcp.token is not None
    assert mcp.token.startswith("mcp_")
    assert mcp.token_type == "mcp"
    assert mcp.token_hint.startswith("mcp_")
    assert edge.token is not None
    assert edge.token.startswith("edge_")
    assert edge.token_type == "edge_agent"
    assert edge.token_hint.startswith("edge_")


@pytest.mark.asyncio
async def test_rotate_token_preserves_type_and_prefixes_new_token(auth_pg_pool) -> None:
    async with auth_pg_pool.acquire() as db:
        await _seed_project(db)
        await _seed_token(
            db,
            token_id=13,
            project_id=1,
            token_type="edge_agent",
            token_hash="old-hash",
            token_hint="oldedge...",
            version=4,
        )

        rotated = await auth_router.rotate_token(13, _={"role": "admin"}, db=db)

        assert rotated.token_type == "edge_agent"
        assert rotated.version == 5
        assert rotated.token is not None
        assert rotated.token.startswith("edge_")
        assert rotated.token_hint.startswith("edge_")

        async with db.execute(
            "SELECT is_active FROM project_tokens WHERE id = ?", (13,)
        ) as cur:
            old = await cur.fetchone()
        assert old["is_active"] == 0


@pytest.mark.asyncio
async def test_trigger_project_index_calls_mcp_server_with_resolved_repo_path(
    auth_pg_pool,
) -> None:
    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, repo_path="D:/Repos/OSAgent")

        with patch.object(
            auth_router, "_resolve_repo_path", return_value="D:/Repos/OSAgent"
        ), patch.object(
            auth_router.mcp_server,
            "index_repo_changes",
            AsyncMock(
                return_value={
                    "status": "queued",
                    "mode": "incremental",
                    "job_id": "job-123",
                    "stream_id": "500-0",
                    "changed_count": 3,
                    "destructive_count": 1,
                }
            ),
        ) as index_repo_changes:
            result = await auth_router.trigger_project_index(
                1, _={"role": "admin"}, db=db
            )

    assert result.project_name == "osagent"
    assert result.repo_path == "D:/Repos/OSAgent"
    assert result.job_id == "job-123"
    index_repo_changes.assert_awaited_once_with(
        repo_path="D:/Repos/OSAgent", project_name="osagent"
    )


@pytest.mark.asyncio
async def test_trigger_project_index_errors_when_repo_path_missing(
    auth_pg_pool,
) -> None:
    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, project_name="missing", project_external_id="MISS123456")

        with patch.object(auth_router, "_resolve_repo_path", return_value=None):
            with pytest.raises(Exception) as exc_info:
                await auth_router.trigger_project_index(
                    1, _={"role": "admin"}, db=db
                )
    assert "Repository path not found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_list_projects_index_status_includes_queue_position_and_eta(
    auth_pg_pool,
) -> None:
    consumer = AsyncMock()
    consumer.get_queue_snapshot.return_value = {
        "pending_jobs": [
            {
                "job_id": "job-001",
                "status": "pending",
                "created_at": "2026-04-19T17:18:41+00:00",
            }
        ],
        "processing_jobs": [
            {
                "job_id": "job-000",
                "status": "processing",
                "updated_at": "2026-04-19T17:18:50+00:00",
            }
        ],
        "avg_duration_sec": 40,
    }
    consumer.get_jobs_by_repo.return_value = [
        {
            "job_id": "job-001",
            "job_type": "index_incremental",
            "repo_path": "D:/Repos/OSAgent",
            "status": "pending",
            "created_at": "2026-04-19T17:18:41+00:00",
            "updated_at": "2026-04-19T17:18:41+00:00",
        }
    ]

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db)

        with patch.object(
            auth_router, "_candidate_repo_paths", return_value=["D:/Repos/OSAgent"]
        ):
            result = await auth_router.list_projects_index_status(
                _={"username": "admin", "role": "admin"},
                db=db,
                consumer=consumer,
            )

    assert len(result) == 1
    assert result[0].latest_job is not None
    assert result[0].latest_job.queue_position == 1
    assert result[0].latest_job.eta_seconds is not None
    assert result[0].latest_job.eta_seconds >= 40
    assert len(result[0].recent_jobs) == 1
    assert result[0].recent_jobs[0].job_id == "job-001"


@pytest.mark.asyncio
async def test_list_projects_index_status_uses_latest_job_across_repo_path_variants(
    auth_pg_pool,
) -> None:
    consumer = AsyncMock()
    consumer.get_queue_snapshot.return_value = {
        "pending_jobs": [],
        "processing_jobs": [],
        "avg_duration_sec": 30,
    }
    consumer.get_jobs_by_repo.side_effect = [
        [
            {
                "job_id": "old-job",
                "job_type": "index_full",
                "repo_path": "D:/Repos/BrowserAgent",
                "status": "done",
                "created_at": "2026-04-19T05:04:43+00:00",
                "updated_at": "2026-04-19T05:05:28+00:00",
            }
        ],
        [
            {
                "job_id": "new-job",
                "job_type": "index_incremental",
                "repo_path": "/repos/browseragent",
                "status": "pending",
                "created_at": "2026-04-19T18:05:28+00:00",
                "updated_at": "2026-04-19T18:05:28+00:00",
            }
        ],
    ]

    async with auth_pg_pool.acquire() as db:
        await _seed_project(
            db, project_name="browseragent", project_external_id="20YYYTOHV8"
        )

        with patch.object(
            auth_router,
            "_candidate_repo_paths",
            return_value=["D:/Repos/BrowserAgent", "/repos/browseragent"],
        ):
            result = await auth_router.list_projects_index_status(
                _={"username": "admin", "role": "admin"},
                db=db,
                consumer=consumer,
            )

    assert len(result) == 1
    assert result[0].latest_job is not None
    assert result[0].latest_job.job_id == "new-job"
    assert result[0].latest_job.status == "pending"
    assert len(result[0].recent_jobs) == 2
    assert [job.job_id for job in result[0].recent_jobs] == ["new-job", "old-job"]


@pytest.mark.asyncio
async def test_list_projects_index_status_uses_stored_repo_path_when_name_differs(
    auth_pg_pool,
) -> None:
    consumer = AsyncMock()
    consumer.get_queue_snapshot.return_value = {
        "pending_jobs": [],
        "processing_jobs": [],
        "avg_duration_sec": 30,
    }
    consumer.get_jobs_by_repo.side_effect = [
        [
            {
                "job_id": "echo-job",
                "job_type": "index_full",
                "repo_path": "/repos/orcasql-agctools-echo-ops",
                "status": "done",
                "created_at": "2026-05-27T08:30:27+00:00",
                "updated_at": "2026-05-27T08:30:28+00:00",
            }
        ],
        [],
    ]

    async with auth_pg_pool.acquire() as db:
        await _seed_project(
            db,
            project_name="Echo-Ops",
            project_external_id="R5Q1OHYLQ7",
            repo_path="/repos/orcasql-agctools-echo-ops",
        )

        with patch.object(
            auth_router,
            "_candidate_repo_paths",
            return_value=["/repos/echo-ops"],
        ):
            result = await auth_router.list_projects_index_status(
                _={"username": "admin", "role": "admin"},
                db=db,
                consumer=consumer,
            )

    assert len(result) == 1
    assert result[0].latest_job is not None
    assert result[0].latest_job.job_id == "echo-job"
    consumer.get_jobs_by_repo.assert_any_await("/repos/orcasql-agctools-echo-ops")


@pytest.mark.asyncio
async def test_recover_project_stale_index_jobs_delegates_to_consumer(
    auth_pg_pool,
) -> None:
    recovered_at = datetime.now(timezone.utc).isoformat()
    consumer = AsyncMock()
    consumer.recover_stale_jobs_by_repo.return_value = [
        {
            "job_id": "job-stale",
            "job_type": "index_full",
            "repo_path": "D:/Repos/OSAgent",
            "status": "failed",
            "created_at": recovered_at,
            "updated_at": recovered_at,
            "error": "Recovered stale processing job after 120s without status updates",
        }
    ]

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, repo_path="D:/Repos/OSAgent")

        with patch.object(
            auth_router, "_resolve_repo_path", return_value="D:/Repos/OSAgent"
        ), patch.object(
            auth_router,
            "_candidate_repo_paths",
            return_value=["D:/Repos/OSAgent"],
        ):
            result = await auth_router.recover_project_stale_index_jobs(
                1,
                _={"role": "admin"},
                db=db,
                consumer=consumer,
            )

    assert result.recovered_count == 1
    assert result.recovered_jobs[0].status == "failed"
    consumer.recover_stale_jobs_by_repo.assert_awaited_once_with(
        ["D:/Repos/OSAgent"],
        auth_router.INDEX_STALE_AFTER_SEC,
    )
