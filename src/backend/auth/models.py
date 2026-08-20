"""Pydantic models for the auth module."""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


# ── Request / response models ──────────────────────────────────────────────


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8)
    role: str = Field(default="developer", pattern="^(admin|developer)$")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not _IDENT_RE.fullmatch(v):
            raise ValueError("username may only contain letters, digits, dot, underscore, and hyphen")
        return v


def _unique_positive_ids(values: list[int]) -> list[int]:
    seen: set[int] = set()
    unique_values: list[int] = []
    for value in values:
        if value <= 0:
            raise ValueError("ids must be positive integers")
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values


def _normalize_repo_path(value: str) -> str:
    return value.strip().replace("\\", "/")


class UserGroupSummary(BaseModel):
    id: int
    group_name: str
    description: str = ""
    created_at: str = ""
    is_active: bool = True


class UserOut(BaseModel):
    id: int
    username: str
    email: str = ""
    role: str
    created_at: str
    is_active: bool
    groups: list[UserGroupSummary] = Field(default_factory=list)


class UserGroupCreate(BaseModel):
    group_name: str = Field(min_length=3, max_length=64)
    description: str = Field(default="", max_length=1000)

    @field_validator("group_name")
    @classmethod
    def validate_group_name(cls, v: str) -> str:
        if not _IDENT_RE.fullmatch(v):
            raise ValueError("group_name may only contain letters, digits, dot, underscore, and hyphen")
        return v


class UserGroupUpdate(BaseModel):
    group_name: str | None = Field(default=None, min_length=3, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool | None = None

    @field_validator("group_name")
    @classmethod
    def validate_optional_group_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _IDENT_RE.fullmatch(v):
            raise ValueError("group_name may only contain letters, digits, dot, underscore, and hyphen")
        return v


class UserGroupOut(BaseModel):
    id: int
    group_name: str
    description: str = ""
    created_at: str
    is_active: bool
    member_count: int = 0
    project_count: int = 0
    project_ids: list[int] = Field(default_factory=list)


class UserGroupIdsUpdate(BaseModel):
    group_ids: list[int] = Field(default_factory=list)

    @field_validator("group_ids")
    @classmethod
    def validate_group_ids(cls, values: list[int]) -> list[int]:
        return _unique_positive_ids(values)


class UserGroupProjectIdsUpdate(BaseModel):
    project_ids: list[int] = Field(default_factory=list)

    @field_validator("project_ids")
    @classmethod
    def validate_project_ids(cls, values: list[int]) -> list[int]:
        return _unique_positive_ids(values)


class UserGroupUserIdsUpdate(BaseModel):
    user_ids: list[int] = Field(default_factory=list)

    @field_validator("user_ids")
    @classmethod
    def validate_user_ids(cls, values: list[int]) -> list[int]:
        return _unique_positive_ids(values)


class UserProfileUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=3, max_length=64)
    email: str | None = Field(default=None, max_length=254)
    current_password: str | None = None
    new_password: str | None = Field(default=None, min_length=8)

    @field_validator("username")
    @classmethod
    def validate_optional_username(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _IDENT_RE.fullmatch(v):
            raise ValueError("username may only contain letters, digits, dot, underscore, and hyphen")
        return v


class AdminUserUpdate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    role: str | None = Field(default=None, pattern="^(admin|developer)$")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not _IDENT_RE.fullmatch(v):
            raise ValueError("username may only contain letters, digits, dot, underscore, and hyphen")
        return v


class AdminUserUpdateResponse(UserOut):
    access_token: str | None = None


class ProjectCreate(BaseModel):
    project_name: str = Field(min_length=3, max_length=64)
    upstream_url: str = Field(default="", max_length=512)
    description: str = Field(default="", max_length=1000)
    repo_path: str = Field(default="", max_length=512)

    @field_validator("project_name")
    @classmethod
    def validate_project_name(cls, v: str) -> str:
        if not _IDENT_RE.fullmatch(v):
            raise ValueError("project_name may only contain letters, digits, dot, underscore, and hyphen")
        return v

    @field_validator("upstream_url")
    @classmethod
    def validate_upstream_url(cls, v: str) -> str:
        if not v:
            return v
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("upstream_url must start with http:// or https://")
        return v

    @field_validator("repo_path")
    @classmethod
    def normalize_repo_path(cls, v: str) -> str:
        return _normalize_repo_path(v)


class ProjectUpdate(BaseModel):
    project_name: str | None = Field(default=None, min_length=3, max_length=64)
    upstream_url: str | None = Field(default=None, max_length=512)
    description: str | None = Field(default=None, max_length=1000)
    repo_path: str | None = Field(default=None, max_length=512)

    @field_validator("project_name")
    @classmethod
    def validate_optional_project_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _IDENT_RE.fullmatch(v):
            raise ValueError("project_name may only contain letters, digits, dot, underscore, and hyphen")
        return v

    @field_validator("upstream_url")
    @classmethod
    def validate_optional_upstream_url(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("upstream_url must start with http:// or https://")
        return v

    @field_validator("repo_path")
    @classmethod
    def normalize_optional_repo_path(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _normalize_repo_path(v)


class ProjectOut(BaseModel):
    id: int
    project_name: str
    project_id: str
    upstream_url: str
    description: str
    repo_path: str = ""
    created_at: str
    is_active: bool


class OutputRuleProfileUpdate(BaseModel):
    rules: dict[str, Any] = Field(default_factory=dict)


class ProjectOutputRuleUpdate(BaseModel):
    base_profile: str = Field(default="concise", min_length=1, max_length=64)
    overrides: dict[str, Any] = Field(default_factory=dict)


class EffectiveOutputRulesOut(BaseModel):
    project_id: int | None = None
    base_profile: str
    resolved: dict[str, Any]
    markdown: str
    version: int
    provenance: dict[str, str]


class UserAccessGroupOut(BaseModel):
    id: int
    group_name: str
    description: str = ""
    created_at: str
    is_active: bool
    projects: list[ProjectOut] = Field(default_factory=list)


class ProjectTokenOut(BaseModel):
    id: int
    project_id: int
    token_type: str
    token_hint: str
    version: int
    created_at: str
    is_active: bool
    # Only populated on creation/rotation (never stored in plaintext):
    token: str | None = None


class GenerateTokenRequest(BaseModel):
    token_type: str = Field(pattern="^mcp$")


class IndexJobStatus(BaseModel):
    """Status of an index job."""
    job_id: str
    job_type: str
    repo_path: str
    status: str  # pending, processing, done, failed, stale
    created_at: str
    updated_at: str
    error: str | None = None
    # Stats from pipeline (if done):
    files: int | None = None
    symbols: int | None = None
    # Queue telemetry (best-effort estimates):
    queue_position: int | None = None
    eta_seconds: int | None = None
    is_stale: bool = False
    age_seconds: int | None = None


class GraphLiveStats(BaseModel):
    """Live node/edge counts from FalkorDB."""
    files: int = 0
    symbols: int = 0
    variables: int = 0
    call_edges: int = 0
    flow_edges: int = 0
    uses_variable_edges: int = 0
    defines_edges: int = 0
    contains_edges: int = 0
    total_nodes: int = 0
    total_edges: int = 0


class ProjectIndexStatus(BaseModel):
    """Index status for a project."""
    project_id: int
    project_name: str
    latest_job: IndexJobStatus | None = None
    recent_jobs: list[IndexJobStatus] = Field(default_factory=list)
    graph_stats: GraphLiveStats | None = None


class ProjectIndexTriggerOut(BaseModel):
    """Result of triggering an index job for a specific project from the admin API."""
    project_id: int
    project_name: str
    repo_path: str
    status: str
    mode: str
    job_id: str | None = None
    stream_id: str | None = None
    changed_count: int = 0
    destructive_count: int = 0
    reason: str | None = None
    message: str | None = None


class ProjectIndexRecoveryOut(BaseModel):
    """Result of recovering stale index jobs for a project."""
    project_id: int
    project_name: str
    repo_path: str
    recovered_count: int
    recovered_jobs: list[IndexJobStatus] = Field(default_factory=list)


class AuditLogOut(BaseModel):
    id: int
    created_at: str
    scope: str
    method: str
    path: str
    status_code: int
    duration_ms: int
    actor_type: str
    actor_id: int | None = None
    actor_name: str | None = None
    project_id: int | None = None
    project_name: str | None = None
    token_id: int | None = None
    client_ip: str | None = None
    user_agent: str | None = None
    query_string: str | None = None
    request_body: str | None = None
    response_error: str | None = None
    details_json: str | None = None
    token_usage_total: int | None = None


class PaginatedAuditOut(BaseModel):
    items: list[AuditLogOut]
    total: int
    page: int
    page_size: int
