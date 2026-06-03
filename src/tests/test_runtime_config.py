from __future__ import annotations

import json
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


def test_runtime_config_persists_modules_and_smtp_without_exposing_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "runtime-config.json"
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", config_path)

    saved = runtime_config.update_runtime_config(
        {
            "modules": {
                "indexer_consumer": {"enabled": False},
                "schedule_worker": {"enabled": True},
            },
            "smtp": {
                "enabled": True,
                "host": "smtp.example.com",
                "port": 465,
                "security": "ssl",
                "username": "mailer@example.com",
                "password": "local-only-secret",
                "from_email": "cga@example.com",
                "from_name": "CGA Mailer",
            },
        }
    )

    assert saved["modules"]["indexer_consumer"]["enabled"] is False
    assert saved["modules"]["schedule_worker"]["enabled"] is True
    assert saved["modules"]["backup_scheduler"]["enabled"] is True
    assert saved["smtp"]["enabled"] is True
    assert saved["smtp"]["host"] == "smtp.example.com"
    assert saved["smtp"]["port"] == 465
    assert saved["smtp"]["security"] == "ssl"
    assert saved["smtp"]["password_set"] is True
    assert "password" not in saved["smtp"]

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["smtp"]["password"] == "local-only-secret"

    reloaded = runtime_config.get_runtime_config()
    assert reloaded["smtp"]["password_set"] is True
    assert "password" not in reloaded["smtp"]


def test_runtime_config_rejects_unknown_module_and_invalid_smtp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", tmp_path / "runtime-config.json")

    with pytest.raises(runtime_config.RuntimeConfigError):
        runtime_config.update_runtime_config({"modules": {"unknown_worker": {"enabled": False}}})

    with pytest.raises(runtime_config.RuntimeConfigError):
        runtime_config.update_runtime_config({"smtp": {"port": 70000}})

    with pytest.raises(runtime_config.RuntimeConfigError):
        runtime_config.update_runtime_config({"smtp": {"security": "tls13"}})


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