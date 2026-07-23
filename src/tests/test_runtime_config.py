from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend import runtime_config
from backend.auth.dependencies import get_current_user, require_admin
from backend.main import app


def test_runtime_config_persists_indexing_repos_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "runtime-config.json"
    repos_root = tmp_path / "workspace-repos"

    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", config_path)

    saved = runtime_config.update_runtime_config(
        {
            "indexing": {
                "repos_root": str(repos_root),
                "default_token_budget": 4096,
                "intelligence_strategy": "hybrid_semantic",
                "parsing_strategy": "config_metadata",
            }
        }
    )
    loaded = runtime_config.get_runtime_config()

    assert saved["indexing"]["repos_root"] == str(repos_root)
    assert saved["indexing"]["default_token_budget"] == 4096
    assert saved["indexing"]["intelligence_strategy"] == "hybrid_semantic"
    assert saved["indexing"]["parsing_strategy"] == "config_metadata"
    assert loaded["indexing"]["repos_root"] == str(repos_root)
    assert loaded["indexing"]["default_token_budget"] == 4096
    assert loaded["indexing"]["intelligence_strategy"] == "hybrid_semantic"
    assert loaded["indexing"]["parsing_strategy"] == "config_metadata"
    assert loaded["indexing"]["repos_root_exists"] is False


def test_runtime_config_rejects_blank_indexing_repos_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", tmp_path / "runtime-config.json")

    with pytest.raises(runtime_config.RuntimeConfigError):
        runtime_config.update_runtime_config({"indexing": {"repos_root": "   "}})


def test_runtime_config_rejects_invalid_indexing_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", tmp_path / "runtime-config.json")

    with pytest.raises(runtime_config.RuntimeConfigError):
        runtime_config.update_runtime_config({"indexing": {"intelligence_strategy": "unsupported"}})

    with pytest.raises(runtime_config.RuntimeConfigError):
        runtime_config.update_runtime_config({"indexing": {"parsing_strategy": "unsupported"}})


def test_runtime_config_rejects_invalid_default_token_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", tmp_path / "runtime-config.json")

    with pytest.raises(runtime_config.RuntimeConfigError):
        runtime_config.update_runtime_config({"indexing": {"default_token_budget": 199}})

    with pytest.raises(runtime_config.RuntimeConfigError):
        runtime_config.update_runtime_config({"indexing": {"default_token_budget": 200001}})

    with pytest.raises(runtime_config.RuntimeConfigError):
        runtime_config.update_runtime_config({"indexing": {"default_token_budget": True}})


def test_runtime_config_uses_environment_smtp_password_without_persisting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "runtime-config.json"
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", config_path)
    monkeypatch.setenv("CGA_SMTP_PASSWORD", "environment-secret")

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
    assert "password" not in raw["smtp"]

    reloaded = runtime_config.get_runtime_config()
    assert reloaded["smtp"]["password_set"] is True
    assert "password" not in reloaded["smtp"]

    delivery = runtime_config.get_smtp_delivery_config()
    assert delivery["password"] == "environment-secret"
    assert "environment-secret" not in json.dumps(reloaded)

    with pytest.raises(runtime_config.RuntimeConfigError, match="CGA_SMTP_PASSWORD"):
        runtime_config.update_runtime_config({"smtp": {"password": "never-persist"}})


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
            json={
                "indexing": {
                    "repos_root": str(repos_root),
                    "default_token_budget": 2600,
                    "intelligence_strategy": "large_monorepo",
                    "parsing_strategy": "tree_sitter_ast",
                }
            },
        )
        reload_response = client.get("/api/admin/runtime-config")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["indexing"]["repos_root"] == str(repos_root)
    assert response.json()["indexing"]["default_token_budget"] == 2600
    assert response.json()["indexing"]["intelligence_strategy"] == "large_monorepo"
    assert response.json()["indexing"]["parsing_strategy"] == "tree_sitter_ast"
    assert reload_response.status_code == 200
    assert reload_response.json()["indexing"]["repos_root"] == str(repos_root)
    assert reload_response.json()["indexing"]["default_token_budget"] == 2600
    assert reload_response.json()["indexing"]["intelligence_strategy"] == "large_monorepo"
    assert reload_response.json()["indexing"]["parsing_strategy"] == "tree_sitter_ast"


def test_indexing_settings_endpoint_allows_authenticated_developer_without_admin_config_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "runtime-config.json"
    repos_root = tmp_path / "repos"
    repos_root.mkdir()

    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", config_path)
    monkeypatch.setattr(main_module.runtime_config, "RUNTIME_CONFIG_PATH", config_path)
    runtime_config.update_runtime_config(
        {
            "indexing": {
                "repos_root": str(repos_root),
                "default_token_budget": 3200,
                "intelligence_strategy": "hybrid_semantic",
                "parsing_strategy": "scip_lsp_semantic",
            }
        }
    )
    app.dependency_overrides[get_current_user] = lambda: {"role": "developer", "username": "developer"}

    try:
        client = TestClient(app)
        response = client.get("/api/indexing/settings")
        admin_response = client.get("/api/admin/runtime-config")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["indexing"]["repos_root"] == str(repos_root)
    assert response.json()["indexing"]["repos_root_exists"] is True
    assert response.json()["indexing"]["default_token_budget"] == 3200
    assert response.json()["indexing"]["intelligence_strategy"] == "hybrid_semantic"
    assert response.json()["indexing"]["parsing_strategy"] == "scip_lsp_semantic"
    assert admin_response.status_code == 403