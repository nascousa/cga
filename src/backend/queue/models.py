from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class JobType(str, Enum):
    INDEX_FULL = "index_full"
    INDEX_INCREMENTAL = "index_incremental"


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRYING = "retrying"
    DONE = "done"
    FAILED = "failed"


class IndexJob(BaseModel):
    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    job_type: JobType
    repo_path: str
    changed_paths: Optional[list[str]] = None
    project_name: Optional[str] = None  # FalkorDB graph name for this project
    max_attempts: int = Field(default=3, ge=1, le=10, strict=True)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


async def validate_job_paths(job: IndexJob) -> IndexJob:
    """Rebind queued paths to an active registration, not a payload assertion."""
    from backend.auth.access import authorize_job_paths

    project_name = (job.project_name or "").strip().lower()
    root, changed_paths = await authorize_job_paths(
        job.repo_path, project_name, job.changed_paths
    )
    return job.model_copy(update={
        "project_name": project_name,
        "repo_path": str(root),
        "changed_paths": changed_paths,
    })
