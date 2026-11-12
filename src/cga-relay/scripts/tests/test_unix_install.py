"""Exercise user-scoped installers without touching the actual login session."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest
import zipfile


SCRIPTS = Path(__file__).resolve().parents[1]


@unittest.skipIf(sys.platform == "win32", "Unix installers require a native shell")
class UnixInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cga-relay-install-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.macos = sys.platform == "darwin"
        self.home = self.root / "home with spaces & unicode-\u00e9"
        self.home.mkdir()
        self.package = self.root / "package"
        (self.package / "bin").mkdir(parents=True)
        (self.package / "scripts").mkdir()
        for name in ("install-unix.sh", "uninstall-unix.sh"):
            shutil.copyfile(SCRIPTS / name, self.package / "scripts" / name)
        core = self.package / "bin" / "cga-relay"
        core.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        core.chmod(0o755)
        self.mock_bin = self.root / "mock-bin"
        self.mock_bin.mkdir()
        for command in ("systemctl", "launchctl"):
            stub = self.mock_bin / command
            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            stub.chmod(0o755)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": str(self.mock_bin) + os.pathsep + os.environ["PATH"],
        }

    def run_script(self, name, *args):
        return subprocess.run(
            ["sh", str(self.package / "scripts" / name), *args],
            env=self.env, cwd=self.root, capture_output=True, text=True, timeout=20,
        )

    def test_cli_upgrade_retains_config_and_private_permissions(self):
        result = self.run_script("install-unix.sh", "--mode", "cli", "--no-start")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.home / ".cga" / "relay.env"
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        config.write_text(config.read_text() + "# retained\n")
        result = self.run_script("install-unix.sh", "--mode", "cli", "--no-start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(config.read_text().endswith("# retained\n"))
        self.assertFalse((self.home / ".config" / "autostart" / "cga-relay.desktop").exists())

    def test_headless_service_and_uninstall_preserve_user_data(self):
        result = self.run_script("install-unix.sh", "--mode", "headless", "--no-start")
        self.assertEqual(result.returncode, 0, result.stderr)
        if self.macos:
            import plistlib
            agent = self.home / "Library" / "LaunchAgents" / "com.nascousa.cga-relay.plist"
            service = plistlib.loads(agent.read_bytes())
            self.assertEqual(service["ProgramArguments"][1], "serve")
            self.assertEqual(service["ProgramArguments"][3], str(self.home / ".cga" / "relay.env"))
        else:
            service = self.home / ".config" / "systemd" / "user" / "cga-relay.service"
            text = service.read_text()
            self.assertIn('" serve --config "', text)
            self.assertIn("UMask=0077", text)
        result = self.run_script("uninstall-unix.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / ".cga" / "relay.env").exists())
        self.assertTrue((self.home / ".cga" / "state").is_dir())

    def test_invalid_mode_fails_without_installing(self):
        result = self.run_script("install-unix.sh", "--mode", "invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid mode", result.stderr)
        self.assertFalse((self.home / ".cga").exists())

    def test_mode_switch_to_cli_removes_previous_startup_entry(self):
        first = self.run_script("install-unix.sh", "--mode", "headless", "--no-start")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_script("install-unix.sh", "--mode", "cli", "--no-start")
        self.assertEqual(second.returncode, 0, second.stderr)
        if self.macos:
            startup = self.home / "Library" / "LaunchAgents" / "com.nascousa.cga-relay.plist"
        else:
            startup = self.home / ".config" / "systemd" / "user" / "cga-relay.service"
        self.assertFalse(startup.exists())

    def test_release_packaging_requires_signing_and_retains_existing_output(self):
        env = {
            key: value for key, value in self.env.items()
            if not key.startswith("CGA_RELAY_")
        }
        output = self.root / "release"
        result = subprocess.run(
            ["sh", str(SCRIPTS / "build-unix-package.sh"), "--release", str(output)],
            env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("signing", result.stderr)
        self.assertFalse(output.exists())
        output.mkdir()
        marker = output / "retained.txt"
        marker.write_text("unchanged")
        result = subprocess.run(
            ["sh", str(SCRIPTS / "build-unix-package.sh"), str(output)],
            env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("never overwritten", result.stderr)
        self.assertEqual(marker.read_text(), "unchanged")


class MacCliInstallerTests(UnixInstallerTests):
    def setUp(self):
        super().setUp()
        self.macos = True
        uname = self.mock_bin / "uname"
        uname.write_text(
            "#!/bin/sh\ncase \"$1\" in -s) echo Darwin;; -m) echo arm64;; "
            "*) exec /usr/bin/uname \"$@\";; esac\n",
            encoding="utf-8",
        )
        uname.chmod(0o755)
        plutil = self.mock_bin / "plutil"
        plutil.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        plutil.chmod(0o755)

    def test_default_install_is_cli_without_an_app_or_autostart(self):
        result = self.run_script("install-unix.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        binary = self.home / ".local" / "share" / "cga-relay" / "bin" / "cga-relay"
        self.assertTrue(binary.is_file())
        self.assertEqual((self.home / ".local" / "bin" / "cga-relay").resolve(), binary)
        self.assertFalse((self.home / "Applications").exists())
        agent = self.home / "Library" / "LaunchAgents" / "com.nascousa.cga-relay.plist"
        self.assertFalse(agent.exists())
        result = self.run_script("uninstall-unix.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".local" / "bin" / "cga-relay").exists())
        self.assertTrue((self.home / ".cga" / "relay.env").exists())

    def test_desktop_mode_fails_before_creating_user_files(self):
        result = self.run_script("install-unix.sh", "--mode", "desktop")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("macOS is CLI-only", result.stderr)
        self.assertFalse((self.home / ".cga").exists())

    def test_macos_package_contains_cli_without_swift_or_app_bundle(self):
        crate = self.root / "repo" / "src" / "cga-relay"
        (crate / "scripts").mkdir(parents=True)
        (crate / "target" / "release").mkdir(parents=True)
        (crate.parents[1] / "LICENSE").write_text("test license")
        (crate / "Cargo.toml").write_text('[package]\nversion = "1.0.0"\n')
        (crate / "target" / "release" / "cga-relay").write_text("#!/bin/sh\nexit 0\n")
        for name in ("build-unix-package.sh", "install-unix.sh", "uninstall-unix.sh"):
            shutil.copyfile(SCRIPTS / name, crate / "scripts" / name)
        stubs = {
            "cargo": "#!/bin/sh\nexit 0\n",
            "codesign": "#!/bin/sh\nexit 0\n",
            "ditto": (
                "#!/usr/bin/env python3\nimport pathlib, sys, zipfile\n"
                "source = pathlib.Path(sys.argv[-2])\n"
                "with zipfile.ZipFile(sys.argv[-1], 'w') as archive:\n"
                " for path in source.rglob('*'):\n"
                "  if path.is_file(): archive.write(path, path.relative_to(source.parent))\n"
            ),
            "shasum": (
                "#!/usr/bin/env python3\nimport hashlib, pathlib, sys\n"
                "for name in sys.argv[3:]:\n"
                " print(hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest() + '  ' + name)\n"
            ),
        }
        for name, script in stubs.items():
            path = self.mock_bin / name
            path.write_text(script, encoding="utf-8")
            path.chmod(0o755)
        env = {key: value for key, value in self.env.items() if key != "CARGO_TARGET_DIR"}
        output = self.root / "candidate-output"
        result = subprocess.run(
            ["sh", str(crate / "scripts" / "build-unix-package.sh"), str(output)],
            env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
            names = archive.getnames()
            instructions = next(name for name in names if name.endswith("/INSTALL.txt"))
            self.assertIn(b"Install: sh scripts/install-unix.sh --mode cli", archive.extractfile(instructions).read())
        self.assertTrue(any(name.endswith("/bin/cga-relay") for name in names))
        self.assertFalse(any(".app/" in name or name.endswith(".swift") for name in names))
        with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
            self.assertEqual(set(archive.namelist()), {
                name for name in names if not any(other.startswith(name + "/") for other in names)
            })


if __name__ == "__main__":
    unittest.main()
