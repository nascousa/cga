from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
import re
from collections.abc import Iterator

from fastapi import HTTPException


RESERVED_GRAPH_PREFIX = "__cga_"
BRANCH_GRAPH_PREFIX = "__cga_ref_v2__"
DEFAULT_REFS = frozenset({"", "main", "master", "default"})


def validate_project_graph_name(project_name: str) -> str:
    name = project_name.strip().lower()
    if name.startswith(RESERVED_GRAPH_PREFIX):
        raise ValueError("Project name is in a reserved graph namespace")
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name) is None:
        raise ValueError("Invalid registered project graph name")
    return name


def is_default_ref(ref_id: str | None) -> bool:
    return (ref_id or "").strip() in DEFAULT_REFS


def branch_graph_name(project_id: str, ref_id: str) -> str:
    if not project_id.strip() or is_default_ref(ref_id):
        raise ValueError("A project ID and non-default ref are required")
    return f"{project_branch_prefix(project_id)}{sha256(ref_id.strip().encode('utf-8')).hexdigest()}"


def project_branch_prefix(project_id: str) -> str:
    if not project_id.strip():
        raise ValueError("A project ID is required")
    return f"{BRANCH_GRAPH_PREFIX}{sha256(project_id.encode('utf-8')).hexdigest()}__"


@dataclass(frozen=True)
class ProjectScope:
    project_id: str
    project_db_id: int
    project_name: str
    repo_path: str

    @property
    def graph_name(self) -> str:
        return validate_project_graph_name(self.project_name)


_current_project_scope: ContextVar[ProjectScope | None] = ContextVar(
    "current_project_scope", default=None
)
_current_ref: ContextVar[str] = ContextVar("current_project_ref", default="")


_current_project_external_id: ContextVar[str] = ContextVar(
    "current_project_external_id",
    default="",
)

_current_project_db_id: ContextVar[int] = ContextVar(
    "current_project_db_id",
    default=0,
)


def require_project_scope() -> ProjectScope:
    scope = _current_project_scope.get()
    if scope is None or not scope.project_id or scope.project_db_id <= 0:
        raise HTTPException(status_code=403, detail="Authenticated project scope is required")
    try:
        scope.graph_name
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return scope


def authorized_graph_name(project_name: str | None = None) -> str:
    from backend.graph.registry import _current_project_name

    scope = require_project_scope()
    ref = _current_ref.get()
    expected = scope.graph_name if is_default_ref(ref) else branch_graph_name(scope.project_id, ref)
    requested = (project_name or _current_project_name.get()).strip().lower()
    if requested != expected:
        raise HTTPException(status_code=403, detail="Graph is not owned by the authenticated project/ref")
    return expected


@contextmanager
def bind_project_scope(scope: ProjectScope) -> Iterator[None]:
    """Bind a DB-resolved scope, also usable by trusted internal admin callers."""
    from backend.graph.registry import _current_project_name

    name_token = _current_project_name.set(scope.graph_name)
    scope_token = _current_project_scope.set(scope)
    id_token = _current_project_external_id.set(scope.project_id)
    db_token = _current_project_db_id.set(scope.project_db_id)
    ref_token = _current_ref.set("")
    try:
        yield
    finally:
        _current_ref.reset(ref_token)
        _current_project_db_id.reset(db_token)
        _current_project_external_id.reset(id_token)
        _current_project_scope.reset(scope_token)
        _current_project_name.reset(name_token)


@contextmanager
def bind_project_ref(ref_id: str) -> Iterator[str]:
    """Select only a ref derived from the current authenticated project ID."""
    from backend.graph.registry import _current_project_name

    scope = require_project_scope()
    ref = ref_id.strip()
    graph_name = scope.graph_name if is_default_ref(ref) else branch_graph_name(scope.project_id, ref)
    name_token = _current_project_name.set(graph_name)
    ref_token = _current_ref.set(ref)
    try:
        yield graph_name
    finally:
        _current_ref.reset(ref_token)
        _current_project_name.reset(name_token)