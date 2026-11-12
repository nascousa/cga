"""Install and exercise a built package inside an isolated user home."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 smoke_unix_package.py EXTRACTED_PACKAGE_DIRECTORY")
    package = Path(sys.argv[1]).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="cga-relay-package-") as directory:
        home = Path(directory)
        env = {**os.environ, "HOME": str(home)}
        subprocess.run(
            ["sh", str(package / "scripts" / "install-unix.sh"),
             "--mode", "cli", "--project-root", str(home)],
            env=env, cwd=home, check=True, timeout=30,
        )
        binary = home / ".local" / "bin" / "cga-relay"
        subprocess.run([str(binary), "--help"], env=env, check=True, timeout=10)
        config = home / ".cga" / "relay.env"
        with tempfile.TemporaryFile() as logs:
            process = subprocess.Popen(
                [str(binary), "serve", "--config", str(config)],
                env=env, stdout=logs, stderr=logs,
            )
            try:
                address_file = home / ".cga" / "state" / "settings-url.txt"
                deadline = time.monotonic() + 10
                while True:
                    if process.poll() is not None:
                        logs.seek(0)
                        raise RuntimeError(logs.read().decode("utf-8", errors="replace"))
                    if address_file.exists():
                        url = address_file.read_text().strip()
                        if url:
                            break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Installed gateway never published its loopback URL")
                    time.sleep(0.05)
                status_url = url.rsplit("/", 1)[0] + "/status.json"
                with urllib.request.urlopen(status_url, timeout=5) as response:
                    status = json.load(response)
                if status.get("session_configured") is not False:
                    raise AssertionError("A fresh installation must not have account credentials")
                with urllib.request.urlopen(url, timeout=5) as response:
                    if response.status != 200:
                        raise AssertionError("Installed settings page is unavailable")
                print("Installed package: CLI, config, loopback settings and status PASS")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
