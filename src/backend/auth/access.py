"""Project access helpers shared by account-authenticated routes."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from fastapi import HTTPException, status

from backend import runtime_config
from backend.auth import pgshim
from backend.auth.context import (
    BRANCH_GRAPH_PREFIX,
    ProjectScope,
    authorized_graph_name,
    bind_project_scope,
    project_branch_prefix,
    require_project_scope,
    validate_project_graph_name,
)
from backend.auth.pgshim import Connection
from backend.indexer.paths import RepositoryPathError, resolve_changed_path, resolve_repo_root


def registered_project_scope(project: dict) -> ProjectScope:
    """Resolve repository authority from a DB record, never from request arguments."""
    try:
        name = validate_project_graph_name(str(project["project_name"]))
        project_id = str(project["project_id"]).strip()
        project_db_id = int(project["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Invalid registered project scope") from exc
    if not project_id or project_db_id <= 0:
        raise HTTPException(status_code=403, detail="Invalid registered project scope")
    repo_path = str(project.get("repo_path") or "").strip()
    if not repo_path:
        repo_path = _discover_registered_repo(name)
    return ProjectScope(project_id, project_db_id, name, repo_path)


def _discover_registered_repo(project_name: str) -> str:
    name_key = "".join(ch for ch in project_name if ch.isalnum())
    roots = runtime_config.get_indexing_repo_search_roots([
        Path(__file__).resolve().parents[4],
        Path("/repos"),
        Path("D:/Repos"),
        Path("d:/repos"),
    ])
    for root in roots:
        try:
            resolved_parent = root.resolve(strict=True)
            exact: set[Path] = set()
            compatible: set[Path] = set()
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                resolved = child.resolve(strict=True)
                if not resolved.is_relative_to(resolved_parent):
                    continue
                if child.name.casefold() == project_name.casefold():
                    exact.add(resolved)
                elif "".join(ch for ch in child.name.casefold() if ch.isalnum()) == name_key:
                    compatible.add(resolved)
            matches = exact or compatible
            if len(matches) > 1:
                raise HTTPException(status_code=403, detail="Repository registration is ambiguous; configure repo_path")
            if matches:
                return str(next(iter(matches)))
        except OSError:
            continue
    return ""


def authorized_repo_root(repo_path: str | None = None) -> Path:
    """Require the request's root to equal the current project's registered root."""
    scope = require_project_scope()
    if not scope.repo_path:
        raise HTTPException(status_code=403, detail="Project repository root is not registered")
    try:
        allowed = resolve_repo_root(scope.repo_path)
        if repo_path is not None and resolve_repo_root(repo_path) != allowed:
            raise RepositoryPathError("Repository root does not match the authenticated project")
        return allowed
    except RepositoryPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def authorized_file_path(file_path: str, *, repo_path: str | None = None) -> Path:
    scope = require_project_scope()
    root = authorized_repo_root(repo_path)
    try:
        return Path(resolve_changed_path(scope.repo_path, root, file_path))
    except RepositoryPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _validated_project_paths(
    repo_path: str, root: Path, changed_paths: Sequence[str] | None
) -> tuple[Path, list[str] | None]:
    if changed_paths is None:
        return root, None
    if isinstance(changed_paths, (str, bytes)) or not isinstance(changed_paths, Sequence):
        raise HTTPException(status_code=400, detail="changed_paths must be a sequence of filenames")
    try:
        safe_paths = [resolve_changed_path(repo_path, root, path) for path in changed_paths]
    except RepositoryPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return root, safe_paths


def authorize_project_paths(
    repo_path: str,
    changed_paths: Sequence[str] | None = None,
    *,
    graph_name: str | None = None,
) -> tuple[Path, list[str] | None]:
    """Synchronous producer validation using a DB-bound authenticated scope.

    ``None`` means full indexing; the root and graph are still authorized.
    Validation is eager so no prefix of a rejected batch can be published.
    """
    authorized_graph_name(graph_name)
    root = authorized_repo_root(repo_path)
    return _validated_project_paths(repo_path, root, changed_paths)


def project_owns_graph(scope: ProjectScope, graph_name: str) -> bool:
    if graph_name == scope.graph_name:
        return True
    prefix = project_branch_prefix(scope.project_id)
    suffix = graph_name.removeprefix(prefix)
    return (
        graph_name.startswith(prefix)
        and len(suffix) == 64
        and all(char in "0123456789abcdef" for char in suffix)
    )


async def authorized_job_repo_root(repo_path: str, graph_name: str | None) -> Path:
    """Revalidate a queued job against active DB registrations in a worker.

    This has no request-context dependency. Old jobs, arbitrary graph overrides,
    and stale registrations do not become trusted merely by surviving in Redis.
    Branch graphs carry a hash of the immutable project ID, not its display name.
    """
    if not graph_name:
        raise HTTPException(status_code=403, detail="Index job has no registered project")
    async with pgshim.get_pool().acquire() as db:
        if graph_name.startswith(BRANCH_GRAPH_PREFIX):
            query = "SELECT id, project_id, project_name, repo_path FROM projects WHERE is_active = 1"
            params = ()
        else:
            try:
                name = validate_project_graph_name(graph_name)
            except ValueError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            query = "SELECT id, project_id, project_name, repo_path FROM projects WHERE lower(project_name) = ? AND is_active = 1"
            params = (name,)
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
    candidates = []
    for row in rows:
        record = dict(row)
        try:
            name = validate_project_graph_name(str(record["project_name"]))
        except (KeyError, ValueError):
            continue
        identity = ProjectScope(str(record["project_id"]), int(record["id"]), name, "")
        if project_owns_graph(identity, graph_name):
            candidates.append(record)
    if len(candidates) != 1:
        raise HTTPException(status_code=403, detail="Index job graph ownership is not uniquely registered")
    with bind_project_scope(registered_project_scope(candidates[0])):
        return authorized_repo_root(repo_path)


async def authorize_job_paths(
    repo_path: str,
    graph_name: str | None,
    changed_paths: Sequence[str] | None = None,
) -> tuple[Path, list[str] | None]:
    """Revalidate a consumer payload before entering its graph-writing thread."""
    root = await authorized_job_repo_root(repo_path, graph_name)
    return _validated_project_paths(repo_path, root, changed_paths)


async def project_access_control_enabled(db: Connection) -> bool:
    async with db.execute("SELECT 1 FROM user_groups WHERE is_active = 1 LIMIT 1") as cur:
        return await cur.fetchone() is not None


async def accessible_project_ids(db: Connection, user: dict) -> set[int] | None:
    if user.get("role") == "admin":
        return None
    if not await project_access_control_enabled(db):
        return None

    async with db.execute(
        """
        SELECT DISTINCT pga.project_id
        FROM user_group_members ugm
        JOIN user_groups ug ON ug.id = ugm.group_id AND ug.is_active = 1
        JOIN project_group_access pga ON pga.group_id = ug.id
        WHERE ugm.user_id = ?
        ORDER BY pga.project_id
        """,
        (int(user["id"]),),
    ) as cur:
        rows = await cur.fetchall()
    return {int(row["project_id"]) for row in rows}


async def user_can_access_project(db: Connection, user: dict, project_db_id: int) -> bool:
    allowed_project_ids = await accessible_project_ids(db, user)
    if allowed_project_ids is None:
        return True
    return int(project_db_id) in allowed_project_ids


async def require_project_access(db: Connection, user: dict, project_db_id: int) -> None:
    if await user_can_access_project(db, user, project_db_id):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project access denied")


def id_filter_sql(column_name: str, ids: set[int]) -> tuple[str, tuple[int, ...]]:
    if not ids:
        return "1 = 0", ()
    ordered_ids = tuple(sorted(ids))
    placeholders = ",".join("?" for _ in ordered_ids)
    return f"{column_name} IN ({placeholders})", ordered_ids