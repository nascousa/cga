"""Shared verification used independently by the API and host executor."""
from __future__ import annotations

import re
from pathlib import Path

import jwt

from backend.maintenance.history import version_tuple


def verify_manifest(token: str, key_path: Path, current_version: str) -> dict:
    try:
        claims = jwt.decode(
            token, key_path.read_text(encoding="ascii"), algorithms=["PS384"],
            audience="cga-updater", issuer="nascousa/cga",
            options={"require": ["exp", "iat", "iss", "aud", "version", "image",
                                 "minimum_version", "schema", "platform", "notes"]},
        )
    except jwt.PyJWTError as exc:
        raise ValueError(f"Trusted update manifest verification failed: {exc}") from exc
    if not isinstance(claims["notes"], str) or not 1 <= len(claims["notes"]) <= 50000:
        raise ValueError("Signed release notes are missing or exceed the supported size.")
    if version_tuple(claims["version"]) <= version_tuple(current_version):
        raise ValueError("Updates must be newer than the installed stable version.")
    if version_tuple(current_version) < version_tuple(claims["minimum_version"]):
        raise ValueError("This update requires an intermediate version.")
    if claims["schema"] != {"auth": 1, "work_briefing": 1, "graph": 2, "runtime_config": 1}:
        raise ValueError("Automatic database/schema migrations are not supported; use a planned upgrade.")
    if claims["platform"] != "docker-compose":
        raise ValueError("Unsupported update installation type.")
    if not isinstance(claims["image"], str) or not re.fullmatch(
        r"ghcr\.io/nascousa/cga-api@sha256:[0-9a-f]{64}", claims["image"]
    ):
        raise ValueError("Only digest-pinned official CGA API images can be installed.")
    return claims
