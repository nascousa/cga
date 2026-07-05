from __future__ import annotations

from backend.auth.models import ProjectCreate, ProjectUpdate


def test_project_payloads_normalize_windows_repo_path() -> None:
    create = ProjectCreate(project_name="hermes-agent", repo_path=r"D:\Repos\ARKSOFT\Harness\hermes")
    update = ProjectUpdate(repo_path=r" D:\Repos\ARKSOFT\Harness\hermes ")

    assert create.repo_path == "D:/Repos/ARKSOFT/Harness/hermes"
    assert update.repo_path == "D:/Repos/ARKSOFT/Harness/hermes"