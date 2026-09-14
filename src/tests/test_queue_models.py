"""Unit tests for queue message models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.queue.models import IndexJob, JobType, JobStatus


def test_index_full_job_defaults():
    job = IndexJob(job_type=JobType.INDEX_FULL, repo_path="/repo/myproject")
    assert job.job_type == JobType.INDEX_FULL
    assert job.repo_path == "/repo/myproject"
    assert job.changed_paths is None
    assert len(job.job_id) == 36  # UUID4
    assert "T" in job.created_at  # ISO timestamp
    assert job.max_attempts == 3


def test_index_incremental_job():
    job = IndexJob(
        job_type=JobType.INDEX_INCREMENTAL,
        repo_path="/repo/myproject",
        changed_paths=["src/a.py", "src/b.py"],
    )
    assert job.job_type == JobType.INDEX_INCREMENTAL
    assert job.changed_paths == ["src/a.py", "src/b.py"]


def test_job_round_trip_json():
    job = IndexJob(
        job_type=JobType.INDEX_INCREMENTAL,
        repo_path="/repo",
        changed_paths=["x.py"],
    )
    restored = IndexJob.model_validate_json(job.model_dump_json())
    assert restored.job_id == job.job_id
    assert restored.job_type == job.job_type
    assert restored.changed_paths == job.changed_paths


def test_job_status_enum():
    assert JobStatus.PENDING == "pending"
    assert JobStatus.RETRYING == "retrying"
    assert JobStatus.DONE == "done"


@pytest.mark.parametrize("max_attempts", [0, -1, 11, 1.5, True, "3"])
def test_retry_budget_is_bounded(max_attempts):
    with pytest.raises(ValidationError):
        IndexJob(job_type=JobType.INDEX_FULL, repo_path=r"D:\repo", max_attempts=max_attempts)


def test_legacy_payload_gets_bounded_retry_default():
    job = IndexJob.model_validate_json(
        '{"job_type":"index_full","repo_path":"legacy","job_id":"legacy-id"}'
    )
    assert job.max_attempts == 3


def test_custom_retry_budget_round_trip():
    job = IndexJob(job_type=JobType.INDEX_FULL, repo_path=r"D:\repo", max_attempts=5)
    assert IndexJob.model_validate_json(job.model_dump_json()).max_attempts == 5
