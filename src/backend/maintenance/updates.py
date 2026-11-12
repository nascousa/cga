from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
import structlog

from backend.maintenance.history import version_tuple
from backend.maintenance.update_manifest import verify_manifest
from backend.maintenance.spool import atomic_json, queue_lock, pending

log = structlog.get_logger()
_cache: dict = {}
_lock = asyncio.Lock()
RELEASE_API = "https://api.github.com/repos/nascousa/cga/releases/latest"


def state_directory() -> Path:
    configured = os.getenv("CGA_UPDATE_STATE_DIR", "").strip()
    if not configured:
        raise ValueError("Host updater is not configured. Configure a dedicated shared update-state directory.")
    return Path(configured)


def executor_status() -> dict:
    if os.getenv("CGA_UPDATE_SINGLE_PROCESS") != "1":
        return {"ready": False, "reason": "Confirmed updates require an explicitly configured single-process API installation."}
    try:
        root = state_directory()
    except ValueError as exc:
        return {"ready": False, "reason": str(exc)}
    status_path = root / "status.json"
    if not status_path.is_file():
        return {"ready": False, "reason": "No host updater heartbeat. Start the configured host executor."}
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        ready = time.time() - float(status["heartbeat"]) < 90 and status.get("accepting_requests") is True
        return {"ready": ready, "reason": "" if ready else status.get("message", "Host updater unavailable or busy."), "job": status}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("updates.executor_status_invalid", error=str(exc))
        return {"ready": False, "reason": "Host updater status is unreadable; inspect the host updater."}


async def _download_manifest(version: str) -> str:
    url = f"https://github.com/nascousa/cga/releases/download/v{version}/update-manifest.jwt"
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 65536:
                    raise ValueError("Update manifest exceeds 64 KiB.")
    return content.decode("ascii").strip()


async def check(current_version: str, *, force: bool = False) -> dict:
    async with _lock:
        if not force and _cache.get("current_version") == current_version and time.time() - _cache.get("_time", 0) < 600:
            return {key: value for key, value in _cache.items() if not key.startswith("_")} | {"executor": executor_status()}
        result = {"current_version": current_version, "checked_at": datetime.now(timezone.utc).isoformat(),
                  "available": False, "installable": False, "channel": "stable"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(RELEASE_API, headers={"Accept": "application/vnd.github+json"})
                response.raise_for_status()
                if len(response.content) > 1024 * 1024:
                    raise ValueError("Release metadata exceeds 1 MiB.")
                release = response.json()
            if release.get("draft") or release.get("prerelease"):
                raise ValueError("The stable release endpoint returned an unpublished or prerelease version.")
            version = str(release["tag_name"]).removeprefix("v")
            available = version_tuple(version) > version_tuple(current_version)
            result.update(status="ok", latest_version=version, available=available,
                          release_url=f"https://github.com/nascousa/cga/releases/tag/v{version}",
                          notes=str(release.get("body") or "No release notes supplied.")[:50000])
            if not available:
                result["reason"] = "Running a newer local build." if version_tuple(version) < version_tuple(current_version) else "No newer stable version."
            elif os.getenv("CGA_PINNED_VERSION", "").strip():
                result["reason"] = "This installation is version-pinned; automatic installation is disabled."
            else:
                key = os.getenv("CGA_UPDATE_PUBLIC_KEY", "").strip()
                if not key:
                    result["reason"] = "A trusted publisher public key has not been configured."
                else:
                    token = await _download_manifest(version)
                    claims = verify_manifest(token, Path(key), current_version)
                    if claims["version"] != version:
                        raise ValueError("Signed manifest version does not match the published release.")
                    result.update(installable=True, image=claims["image"], notes=claims["notes"], _manifest=token)
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, jwt.InvalidTokenError) as exc:
            log.warning("updates.check_failed", error=str(exc))
            result.update(status="error", reason=f"Update check/verification failed: {exc}")
        _cache.clear()
        _cache.update(result, _time=time.time())
        return {key: value for key, value in result.items() if not key.startswith("_")} | {"executor": executor_status()}


async def request_install(current_version: str, version: str, image: str, actor: str) -> dict:
    result = await check(current_version, force=True)
    if not result["installable"] or not result["executor"]["ready"]:
        raise ValueError(result.get("reason") or result["executor"].get("reason") or "Update is not installable.")
    if (version, image) != (result["latest_version"], result["image"]):
        raise ValueError("Available update changed. Review the current release and confirm again.")
    root = state_directory()
    token = await _download_manifest(version)
    claims = verify_manifest(token, Path(os.environ["CGA_UPDATE_PUBLIC_KEY"]), current_version)
    if (claims["version"], claims["image"]) != (version, image):
        raise ValueError("Signed release changed. Review it again.")
    request = {
        "id": uuid.uuid4().hex, "actor": actor, "current_version": current_version,
        "version": version, "image": image, "manifest": token,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    }
    with queue_lock(root):
        if pending():
            raise ValueError("An update is already queued or active.")
        atomic_json(root / "request.json", request)
    return {"status": "queued", "id": request["id"], "version": version}


async def drain_loop(app) -> None:
    from backend.plugins.host import catalog_status

    while True:
        try:
            if pending():
                consumer = getattr(app.state, "consumer", None)
                plugins = catalog_status()["items"]
                idle = consumer is not None and consumer._idle.is_set()
                idle = idle and not any(plugin["active_executions"] for plugin in plugins)
                idle = idle and getattr(app.state, "maintenance_inflight", 0) == 0
                atomic_json(state_directory() / "drain.json", {
                    "ready": idle, "version": app.version, "heartbeat": time.time(),
                })
        except Exception:
            log.exception("updates.drain_probe_failed")
        await asyncio.sleep(1)


def cancel_queued() -> dict:
    root = state_directory()
    with queue_lock(root):
        if (root / "active.json").exists():
            raise ValueError("The host already claimed this update; inspect status rather than interrupting it.")
        request = root / "request.json"
        if not request.exists():
            raise ValueError("No queued update.")
        request.rename(root / f"cancelled-{uuid.uuid4().hex}.json")
    return {"status": "cancelled"}
