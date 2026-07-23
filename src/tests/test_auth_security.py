from __future__ import annotations

from pathlib import Path

import jwt
import pytest
from jwt import InvalidTokenError

from backend.auth import router as auth_router
from backend.auth import security


ROOT = Path(__file__).resolve().parents[2]


def test_jwt_secret_requires_at_least_256_bits() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        security._resolve_jwt_secret("too-short")


def test_jwt_secret_uses_secure_process_local_fallback() -> None:
    first = security._resolve_jwt_secret(None)
    second = security._resolve_jwt_secret("")

    assert len(first.encode("utf-8")) >= 32
    assert len(second.encode("utf-8")) >= 32
    assert first != second


def test_access_token_round_trip_uses_pyjwt_error_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security, "JWT_SECRET", "test-jwt-key-with-at-least-32-bytes")
    token = security.create_access_token({"sub": "developer", "role": "admin"})

    assert security.decode_access_token(token)["sub"] == "developer"
    assert security.JWTError is InvalidTokenError

    with pytest.raises(InvalidTokenError):
        security.decode_access_token("not-a-jwt")


def test_microsoft_account_claims_are_read_without_signature_verification() -> None:
    id_token = jwt.encode(
        {
            "oid": "account-123",
            "name": "CGA Developer",
        },
        "external-provider-key-with-32-bytes",
        algorithm="HS256",
    )

    account = auth_router._microsoft_account_from_token(
        {"id_token": id_token},
        {"id": "fallback", "username": "fallback-user"},
    )

    assert account == ("account-123", "CGA Developer")


def test_auth_requirements_exclude_python_ecdsa_dependency() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()

    assert "pyjwt[crypto]" in requirements
    assert "python-jose" not in requirements
    assert "ecdsa" not in requirements