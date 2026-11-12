"""Build the joint standards/approval/SDK kit without publishing or approving it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def build(root: Path, output: Path, expected_version: str | None = None) -> Path:
    sdk = root / "sdk/cga-plugin-sdk"
    version = tomllib.loads((sdk / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if expected_version is not None and version != expected_version:
        raise ValueError("Requested version differs from the SDK package version.")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output must be empty; released artifacts are never overwritten.")
    documents = sorted((root / "docs/plugins").glob(f"CGA-Plugin-*-v{version}-*.md"))
    if len(documents) != 4:
        raise ValueError("Both language variants of the standard and approval policy are required.")
    inputs = [sdk / name for name in ("pyproject.toml", "README.md", "LICENSE", "src/cga_plugin_sdk/LICENSE")]
    inputs += sorted((sdk / "src").rglob("*.py")) + sorted((sdk / "tests").rglob("*.py"))
    if any(path.is_symlink() or not path.is_file() for path in inputs + documents):
        raise ValueError("SDK inputs must be real files.")
    source_commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    paths = ["sdk/cga-plugin-sdk", "docs/plugins", "src/scripts/build_plugin_sdk.py",
             "src/scripts/check_plugin_approval.py", ".github/workflows/plugin-sdk.yml",
             ".github/workflows/plugin-sdk-release.yml"]
    dirty = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain", "--", *paths], text=True)
    with tempfile.TemporaryDirectory(prefix="cga-plugin-sdk-build-") as directory:
        temporary = Path(directory)
        project = temporary / "sdk"
        for path in inputs:
            target = project / path.relative_to(sdk)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        artifacts = temporary / "artifacts"
        subprocess.run([sys.executable, "-m", "build", "--no-isolation",
                        "--outdir", str(artifacts), str(project)], check=True)
        source = str(sdk / "src")
        with (artifacts / "plugin.schema.json").open("w", encoding="utf-8") as schema:
            subprocess.run([sys.executable, "-c",
                            "import json,sys;sys.path.insert(0,sys.argv[1]);"
                            "from cga_plugin_sdk import PluginManifest;"
                            "print(json.dumps(PluginManifest.model_json_schema(),indent=2))", source],
                           check=True, stdout=schema)
        example = temporary / "sample-inspector"
        subprocess.run([sys.executable, "-m", "cga_plugin_sdk", "init", str(example),
                        "--id", "sample_inspector"], check=True,
                       env={**os.environ, "PYTHONPATH": source})
        kit = artifacts / f"CGA-Plugin-Development-Kit-v{version}-en.zip"
        members = {f"packages/{path.name}": path.read_bytes() for path in artifacts.iterdir() if path.is_file()}
        members.update({f"standards/{path.name}": path.read_bytes() for path in documents})
        members[f"CGA-Plugin-Developer-Guide-v{version}-en.md"] = (sdk / "README.md").read_bytes()
        members["LICENSE"] = (sdk / "LICENSE").read_bytes()
        members.update({f"examples/sample-inspector/{path.name}": path.read_bytes()
                        for path in example.iterdir()})
        for filename in ("build_plugin_sdk.py", "check_plugin_approval.py"):
            members[f"tools/{filename}"] = (root / "src/scripts" / filename).read_bytes()
        members["release.json"] = json.dumps({
            "version": version, "source_commit": source_commit,
            "source_state": "working-tree" if dirty.strip() else "committed",
            "approval": "Independent GitHub approval and provenance must be verified separately.",
            "dependency_requirements": tomllib.loads((sdk / "pyproject.toml").read_text(
                encoding="utf-8"))["project"]["dependencies"],
            "files": {name: hashlib.sha256(content).hexdigest() for name, content in sorted(members.items())},
        }, indent=2).encode("utf-8")
        with zipfile.ZipFile(kit, "x", zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(members.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        (artifacts / (kit.name + ".sha256")).write_text(
            hashlib.sha256(kit.read_bytes()).hexdigest() + "  " + kit.name + "\n", encoding="utf-8")
        checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                            for path in sorted(artifacts.iterdir()))
        (artifacts / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
        output.mkdir(parents=True, exist_ok=True)
        for path in artifacts.iterdir():
            with (output / path.name).open("xb") as target:
                target.write(path.read_bytes())
    return output / kit.name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version")
    args = parser.parse_args(argv)
    try:
        print(build(args.root.resolve(), args.output.resolve(), args.version))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"SDK build failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
