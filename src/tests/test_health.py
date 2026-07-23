from fastapi.testclient import TestClient

from backend.main import APP_VERSION, MIN_RELAY_VERSION, _upgrade_status_payload, app


client = TestClient(app)


def test_health_endpoint_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "cga", "name": "Context Graph Agent", "version": APP_VERSION}


def test_mcp_discovery_advertises_crystals_profile_headers() -> None:
    response = client.get("/mcp")

    assert response.status_code == 200
    auth = response.json()["auth"]
    assert auth["crystals_profile"] == {
        "profile": "CRYSTALS-CNSA-2.0",
        "key_establishment": "ML-KEM-1024",
        "signature": "ML-DSA-87",
        "local_transport_scope": "local-ipc",
    }
    assert "X-CGA-Communication-Profile" in auth["required_headers"]


def test_upgrade_status_payload_contains_local_readiness_contract() -> None:
    payload = _upgrade_status_payload()

    assert payload["app_version"] == APP_VERSION
    assert MIN_RELAY_VERSION == "1.30.119"
    assert payload["compatibility"]["min_relay_version"] == MIN_RELAY_VERSION
    assert payload["backup"]["backup_dir"]
    assert payload["schema"] == {
        "auth": 1,
        "work_briefing": 1,
        "graph": 1,
        "runtime_config": 1,
    }
    assert payload["compatibility"]["rollback_mode"] == "restore-pre-upgrade-backup"
    assert payload["readiness"]["checks"]
    assert {check["id"] for check in payload["readiness"]["checks"]} >= {
        "backup",
        "database-schema",
        "graph-schema",
        "relay",
    }
    assert {group["id"] for group in payload["commands"]} == {
        "desktop-bundle",
        "release-compose",
        "source-dev",
    }
