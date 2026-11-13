"""Installation-scoped Docker Compose updater. Never run inside the API container."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.maintenance.spool import QueueLockBusyError, atomic_json, queue_lock  # noqa: E402
from backend.maintenance.update_manifest import verify_manifest  # noqa: E402


def command(args: list[str], timeout: int = 300) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        # Compose output can include environment values; keep it out of API/job status.
        raise RuntimeError(f"{Path(args[0]).name} failed with exit code {result.returncode}; inspect host service logs.")
    return result.stdout


class HostUpdater:
    def __init__(self, config: dict):
        self.config = config
        self.root = Path(config["state_dir"]).resolve(strict=True)
        self.journal_root = Path(config["journal_dir"]).resolve(strict=True)
        if self.journal_root == self.root or self.root in self.journal_root.parents:
            raise ValueError("Recovery journals/backups must be outside the API-writable shared directory.")
        self.compose = Path(config["compose_file"]).resolve(strict=True)
        self.key = Path(config["public_key"]).resolve(strict=True)
        if self.root in self.key.parents or self.root in self.compose.parents:
            raise ValueError("Trusted public key and Compose must not be stored in the API-writable update directory.")
        if self.compose.suffix != ".json":
            raise ValueError("This first updater supports an existing JSON Compose file only.")
        for key in ("project", "service", "container"):
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", config[key]):
                raise ValueError(f"Invalid installation {key}.")
        url = urlparse(config["health_url"])
        if url.scheme != "http" or url.hostname != "127.0.0.1" or url.path != "/health" or not url.port:
            raise ValueError("health_url must be the configured loopback /health URL with an explicit original port.")
        if not isinstance(config["backup_command"], list) or not config["backup_command"]:
            raise ValueError("Configure a trusted host-side backup command.")
        self.health_port = url.port
        self.progress = {"status": "ready", "message": "Waiting for administrator confirmation."}
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def compose_command(self, *args: str) -> list[str]:
        return ["docker", "compose", "--project-name", self.config["project"],
                "--file", str(self.compose), *args, self.config["service"]]

    def inspect(self) -> dict:
        value = json.loads(command(["docker", "inspect", self.config["container"]]))[0]
        labels = value["Config"].get("Labels", {})
        if (labels.get("com.docker.compose.project"), labels.get("com.docker.compose.service")) != (
            self.config["project"], self.config["service"],
        ):
            raise ValueError("The configured container belongs to a different project/service.")
        ports = value["NetworkSettings"].get("Ports", {})
        if not any(binding.get("HostPort") == str(self.health_port)
                   for bindings in ports.values() if bindings for binding in bindings):
            raise ValueError("Health port does not match the existing installation; no port changes are permitted.")
        env = value["Config"].get("Env", [])
        if "CGA_UPDATE_SINGLE_PROCESS=1" not in env:
            raise ValueError("Host updating requires an explicitly configured single-process API.")
        return value

    @staticmethod
    def snapshot(value: dict, environment_keys: list[str] | None = None) -> dict:
        environment = dict(item.split("=", 1) for item in value["Config"]["Env"])
        keys = sorted(environment if environment_keys is None else environment_keys)
        return {
            "Config": {"Image": value["Config"]["Image"], "EnvHash": hashlib.sha256(
                json.dumps({key: environment.get(key) for key in keys}, sort_keys=True).encode()).hexdigest(),
                "EnvKeys": keys},
            "HostConfig": {"PortBindings": value["HostConfig"]["PortBindings"]},
            "Mounts": value["Mounts"],
        }

    def environment_keys(self) -> list[str]:
        resolved = json.loads(command(self.compose_command("config", "--format", "json")))
        return sorted(resolved["services"][self.config["service"]].get("environment", {}))

    def heartbeat(self) -> None:
        atomic_json(self.root / "status.json", {
            **self.progress, "heartbeat": time.time(),
            "accepting_requests": self.progress["status"] in {"ready", "success", "rolled-back", "failed"}
            and not (self.root / "active.json").exists(),
        })

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(10):
            self.heartbeat()

    def progress_to(self, status: str, message: str, **fields) -> None:
        self.progress = {**self.progress, **fields, "status": status, "message": message}
        self.heartbeat()

    def wait_drained(self, version: str, request_id: str) -> None:
        for _ in range(300):
            path = self.root / "drain.json"
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if (data.get("ready") and data.get("version") == version
                        and data.get("request_id") == request_id
                        and 0 <= time.time() - data["heartbeat"] < 5):
                    return
            time.sleep(1)
        raise RuntimeError("Timed out draining active writes, tasks and indexing. No replacement was performed.")

    def health(self, version: str) -> None:
        for _ in range(90):
            try:
                with urlopen(self.config["health_url"], timeout=3) as response:
                    data = json.load(response)
                if data.get("status") == "ok" and data.get("version") == version:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(2)
        raise RuntimeError("Expected runtime version did not become healthy on its original port.")

    def backup(self, job_dir: Path) -> None:
        started = time.time()
        command(self.config["backup_command"], timeout=900)
        for name, source in self.config["backup_files"].items():
            if name not in {"auth.sql.gz", "falkordb.tgz"}:
                raise ValueError("Expected auth.sql.gz and falkordb.tgz backup receipts.")
            path = Path(source).resolve(strict=True)
            if path.stat().st_mtime < started - 2 or not path.stat().st_size:
                raise ValueError("Backup is missing, empty or older than this update attempt.")
            with gzip.open(path, "rb") as stream:
                while stream.read(1024 * 1024):
                    pass
            shutil.copy2(path, job_dir / name)
        if set(self.config["backup_files"]) != {"auth.sql.gz", "falkordb.tgz"}:
            raise ValueError("Both auth and graph backups are required.")

    def verify_runtime(self, version: str, before: dict, expected_image: str | None = None) -> None:
        self.health(version)
        after = self.snapshot(self.inspect(), before["Config"]["EnvKeys"])
        if expected_image and after["Config"]["Image"] != expected_image:
            raise ValueError("Running container does not use the verified target image.")
        if before["Config"]["EnvHash"] != after["Config"]["EnvHash"]:
            raise ValueError("Runtime environment changed during image-only update.")
        if before["HostConfig"]["PortBindings"] != after["HostConfig"]["PortBindings"]:
            raise ValueError("Runtime port bindings changed.")
        def mount_key(mount):
            return (mount["Type"], mount["Source"], mount["Destination"], mount["RW"])
        if sorted(map(mount_key, before["Mounts"])) != sorted(map(mount_key, after["Mounts"])):
            raise ValueError("Runtime mounts changed.")
        probe = (
            "import asyncio,asyncpg;from backend.auth.pgshim import resolve_auth_dsn;"
            "async def_probe_placeholder"
        )
        probe = probe.replace("async def_probe_placeholder", """
async def probe():
 c=await asyncpg.connect(resolve_auth_dsn())
 try:
  await c.fetchval('SELECT 1 FROM projects LIMIT 1')
  await c.fetchval('SELECT 1 FROM maintenance_state LIMIT 1')
 finally:
  await c.close()
asyncio.run(probe())
""")
        command(["docker", "exec", self.config["container"], "python", "-c", probe])

    def rollback(self, job_dir: Path, version: str, before: dict) -> None:
        # This updater permits only schema-compatible images. Never restore an old DB over newer writes.
        original = (job_dir / "compose-original.json").read_bytes()
        temp = self.compose.with_suffix(".rollback.tmp")
        temp.write_bytes(original)
        temp.replace(self.compose)
        command(self.compose_command("up", "-d", "--no-deps", "--pull", "never"))
        self.verify_runtime(version, before)

    def process(self) -> None:
        with queue_lock(self.root):
            request_path = self.root / "request.json"
            if (self.root / "active.json").exists():
                raise ValueError("An interrupted update needs journal recovery before new requests.")
            if not request_path.exists():
                return
            request_path.rename(self.root / "active.json")
        active = self.root / "active.json"
        stopped = False
        job_dir = None
        before = None
        request = json.loads(active.read_text(encoding="utf-8"))
        try:
            if not re.fullmatch(r"[0-9a-f]{32}", request["id"]):
                raise ValueError("Invalid update request identifier.")
            if datetime.fromisoformat(request["expires_at"]) <= datetime.now(timezone.utc):
                raise ValueError("Upgrade confirmation expired. Review and confirm again.")
            claims = verify_manifest(request["manifest"], self.key, request["current_version"])
            if (claims["version"], claims["image"]) != (request["version"], request["image"]):
                raise ValueError("The confirmed target does not match its signed manifest.")
            before = self.snapshot(self.inspect(), self.environment_keys())
            self.health(request["current_version"])
            compose = json.loads(self.compose.read_text(encoding="utf-8"))
            service = compose["services"][self.config["service"]]
            if service.get("image") != before["Config"]["Image"]:
                raise ValueError("Persisted Compose image differs from the running installation.")
            if shutil.disk_usage(self.journal_root).free < 2 * 1024**3:
                raise ValueError("At least 2 GiB free space is required for update staging.")
            job_dir = self.journal_root / request["id"]
            job_dir.mkdir()
            shutil.copy2(self.compose, job_dir / "compose-original.json")
            atomic_json(job_dir / "journal.json", {"request": request, "before": {
                "version": request["current_version"], "image": before["Config"]["Image"],
            }, "phase": "preparing"})
            self.progress_to("preparing", "Downloading verified image; draining active work.",
                             id=request["id"], version=request["version"])
            command(["docker", "pull", claims["image"]], timeout=900)
            self.wait_drained(request["current_version"], request["id"])
            self.progress_to("backing-up", "Brief maintenance outage: stopping only CGA and verifying fresh backups.")
            # Persist the recovery phase before any operation that can stop the old runtime.
            atomic_json(job_dir / "journal.json", {"request": request, "before": before, "phase": "stopping"})
            stopped = True
            command(self.compose_command("stop", "--timeout", "300"))
            self.backup(job_dir)
            service["image"] = claims["image"]
            atomic_json(self.compose, compose)
            self.progress_to("installing", "Starting the confirmed digest on the existing ports.")
            command(self.compose_command("up", "-d", "--no-deps", "--pull", "never"))
            self.verify_runtime(request["version"], before, claims["image"])
            shutil.move(str(active), str(job_dir / "completed.json"))
            self.progress_to("success", "Upgrade verified. Data, environment, mounts and ports preserved.")
        except Exception as exc:
            if stopped and job_dir and before:
                self.progress_to("rolling-back", "Target verification failed; restoring the previous image, not the database.")
                try:
                    self.rollback(job_dir, request["current_version"], before)
                except Exception:
                    self.progress_to("recovery-required", "Rollback failed. Preserve the journal and backups; operator recovery is required.")
                    raise
                shutil.move(str(active), str(job_dir / "failed.json"))
                self.progress_to("rolled-back", str(exc))
            else:
                if job_dir:
                    shutil.move(str(active), str(job_dir / "failed.json"))
                else:
                    active.rename(self.root / f"rejected-{int(time.time_ns())}.json")
                self.progress_to("failed", str(exc))

    def recover(self) -> None:
        active = self.root / "active.json"
        if not active.exists():
            return
        request = json.loads(active.read_text(encoding="utf-8"))
        if not re.fullmatch(r"[0-9a-f]{32}", request["id"]):
            raise ValueError("Invalid recovery request; preserve state for manual inspection.")
        job_dir = self.journal_root / request["id"]
        journal = json.loads((job_dir / "journal.json").read_text(encoding="utf-8"))
        if journal["phase"] == "stopping":
            self.progress_to("rolling-back", "Recovering an interrupted image switch.")
            self.rollback(job_dir, request["current_version"], journal["before"])
        shutil.move(str(active), str(job_dir / "interrupted.json"))
        self.progress_to("rolled-back", "Interrupted update recovered; a new administrator confirmation is required.")

    def run(self, once: bool = False) -> None:
        lock = self.journal_root / "worker.lock"
        handle = lock.open("a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                if not lock.stat().st_size:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.inspect()
            self.recover()
            self.heartbeat()
            self.thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self.thread.start()
            while True:
                try:
                    self.process()
                except QueueLockBusyError:
                    print("Update queue is busy; retrying the next poll.", file=sys.stderr)
                else:
                    if once:
                        return
                time.sleep(2)
        finally:
            self.stop_event.set()
            if self.thread:
                self.thread.join()
            handle.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    updater = HostUpdater(json.loads(args.config.read_text(encoding="utf-8")))
    updater.run(args.once)


if __name__ == "__main__":
    main()
