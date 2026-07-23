"""Security helpers: password hashing, JWT issue/verify, token hashing."""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

JWTError = jwt.InvalidTokenError


def _resolve_jwt_secret(configured: str | None) -> str:
    if configured:
        if len(configured.encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 bytes")
        return configured
    return secrets.token_urlsafe(32)


JWT_SECRET = _resolve_jwt_secret(os.getenv("JWT_SECRET_KEY"))
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

TOKEN_LENGTH = 35  # ADC standard: 35 A-Za-z0-9 chars
TOKEN_PREFIXES = {
    "mcp": "mcp_",
}


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Raises JWTError on failure."""
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


def generate_token() -> str:
    """Generate a cryptographically random 35-char A-Za-z0-9 token."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(TOKEN_LENGTH))


def generate_project_token(token_type: str) -> str:
    """Generate a project token with a stable type prefix and random body."""
    try:
        prefix = TOKEN_PREFIXES[token_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported token_type: {token_type}") from exc
    return prefix + generate_token()


def hash_token(token: str) -> str:
    """SHA-256 hex digest used for DB storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def token_hint(token: str) -> str:
    """First 8 chars shown in the UI for identification."""
    return token[:8] + "…"
