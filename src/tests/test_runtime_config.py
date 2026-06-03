from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend import runtime_config
from backend.auth.dependencies import require_admin
from backend.main import app


def test_runtime_config_persists_indexing_repos_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "runtime-config.json"
    repos_root = tmp_path / "workspace-repos"

    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", config_path)

    saved = runtime_config.update_runtime_config({"indexing": {"repos_root": str(repos_root)}})
    loaded = runtime_config.get_runtime_config()

    assert saved["indexing"]["repos_root"] == str(repos_root)
    assert loaded["indexing"]["repos_root"] == str(repos_root)
    assert loaded["indexing"]["repos_root_exists"] is False


def test_runtime_config_rejects_blank_indexing_repos_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", tmp_path / "runtime-config.json")

    with pytest.raises(runtime_config.RuntimeConfigError):
        runtime_config.update_runtime_config({"indexing": {"repos_root": "   "}})


def test_admin_runtime_config_endpoint_updates_indexing_repos_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "runtime-config.json"
    repos_root = tmp_path / "repos"

    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", config_path)
    monkeypatch.setattr(main_module.runtime_config, "RUNTIME_CONFIG_PATH", config_path)
    app.dependency_overrides[require_admin] = lambda: {"role": "admin", "username": "admin"}

    try:
        client = TestClient(app)
        response = client.patch(
            "/api/admin/runtime-config",
            json={"indexing": {"repos_root": str(repos_root)}},
        )
        reload_response = client.get("/api/admin/runtime-config")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["indexing"]["repos_root"] == str(repos_root)
    assert reload_response.status_code == 200
    assert reload_response.json()["indexing"]["repos_root"] == str(repos_root)