"""Publish a signed, digest-pinned API update only after formal release gates."""
import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.maintenance.history import history, version_tuple  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--minimum-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--notes-output", type=Path, required=True)
    args = parser.parse_args()
    version_tuple(args.version)
    version_tuple(args.minimum_version)
    source = (Path(__file__).resolve().parents[1] / "backend/main.py").read_text(encoding="utf-8")
    current = re.search(r'^APP_VERSION = "([^"]+)"', source, re.MULTILINE).group(1)
    if args.version != current or version_tuple(args.version) < version_tuple(args.minimum_version):
        parser.error("Release/package/minimum versions are inconsistent.")
    if not re.fullmatch(r"ghcr\.io/nascousa/cga-api@sha256:[0-9a-f]{64}", args.image):
        parser.error("Expected an official digest-pinned CGA API image.")
    data = history(args.version)
    if data["unreleased"]["changes"]:
        parser.error("Unreleased change fragments remain.")
    release = next((item for item in data["releases"] if item["version"] == args.version), None)
    if release is None:
        parser.error("The release has no frozen Change Logs entry.")
    secret = os.environ.pop("CGA_UPDATE_SIGNING_KEY", "")
    if not secret:
        parser.error("CGA_UPDATE_SIGNING_KEY is required; unsigned manifests are never published.")
    key = serialization.load_pem_private_key(secret.encode(), password=None)
    if not isinstance(key, RSAPrivateKey) or key.key_size < 3072:
        parser.error("The update signing key must be RSA with at least 3072 bits.")
    notes = release["summary"] + "\n\n" + "\n\n".join(
        f"## {category}\n" + "\n".join(f"- {entry}" for entry in entries)
        for category, entries in release["changes"].items()
    )
    now = datetime.now(timezone.utc)
    token = jwt.encode({
        "iss": "nascousa/cga", "aud": "cga-updater", "iat": now,
        "exp": now + timedelta(days=365), "version": args.version,
        "minimum_version": args.minimum_version, "platform": "docker-compose",
        "image": args.image, "notes": notes,
        "schema": {"auth": 1, "work_briefing": 1, "graph": 2, "runtime_config": 1},
    }, key, algorithm="PS384")
    if len(notes) > 50000 or len(token) > 65536:
        parser.error("Release notes exceed the update manifest size limit; provide a concise user-facing summary.")
    args.output.write_text(token + "\n", encoding="ascii")
    args.notes_output.write_text(notes + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
