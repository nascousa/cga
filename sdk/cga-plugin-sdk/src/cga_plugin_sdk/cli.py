"""Static inspection only: validation and packaging never import plugin code."""
from __future__ import annotations

import argparse
import hashlib
from importlib.resources import files as package_files
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

from pydantic import ValidationError

from .contracts import PluginManifest, resource_path

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
ADAPTER = '''from cga_plugin_sdk import ExecutionResult, ScheduledTask


async def execute(task: ScheduledTask, started_at: str, started_clock: float):
    if not task.project_external_id:
        return ExecutionResult("failed", {}, "A project scope is required.").to_host_result()
    return ExecutionResult("success", {
        "project_id": task.project_external_id,
        "summary": "Read-only example; no external action was performed.",
    }, response_status=200).to_host_result()
'''
EXAMPLE_TEST = '''import asyncio
from types import SimpleNamespace

from plugin import execute


def test_project_scope_is_required():
    result = asyncio.run(execute(SimpleNamespace(project_external_id=None), "", 0))
    assert result[0] == "failed" and result[3]


def test_read_only_result():
    result = asyncio.run(execute(SimpleNamespace(project_external_id="P1"), "", 0))
    assert result[0] == "success" and result[2]["project_id"] == "P1"
'''


def scaffold(destination: Path, plugin_id: str) -> Path:
    manifest = PluginManifest(
        schema_version=1, id=plugin_id, name=plugin_id.replace("_", " ").title(), version="1.0.0",
        sdk_api="cga.first-party.v1", execution_model="first-party-in-process",
        min_cga_version="1.30.132", entrypoint="plugin:execute", license="Apache-2.0",
        permissions=["project.read"], network_hosts=[], secret_env=[], timeout_seconds=30,
        retention_days=7, resources=["plugin.py", "test_plugin.py", "README.md", "LICENSE"],
    )
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "plugin.json").write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (destination / "plugin.py").write_text(ADAPTER, encoding="utf-8")
    (destination / "test_plugin.py").write_text(EXAMPLE_TEST, encoding="utf-8")
    (destination / "LICENSE").write_text(
        package_files("cga_plugin_sdk").joinpath("LICENSE").read_text(encoding="utf-8"), encoding="utf-8")
    (destination / "README.md").write_text(
        f"# {manifest.name}\n\nGenerated first-party integration example; not installed or approved.\n\n"
        "Run `python -m pytest test_plugin.py`. Integrate only through a reviewed CGA source change.\n"
        "The manifest declares permissions; it is not an operating-system sandbox.\n"
        "No external data is retained. The host owns authentication, leases, scheduling and audit.\n",
        encoding="utf-8",
    )
    return destination


def inspect_bundle(root: Path) -> tuple[PluginManifest, dict[str, bytes]]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Plugin root must be a real directory, not a symlink.")
    root = root.resolve()
    manifest_file = root / "plugin.json"
    if not manifest_file.is_file() or manifest_file.is_symlink() or manifest_file.stat().st_size > 65536:
        raise ValueError("Manifest must be a regular file of at most 64 KiB.")
    with manifest_file.open("rb") as source:
        manifest_bytes = source.read(65537)
    if len(manifest_bytes) > 65536:
        raise ValueError("Manifest must be at most 64 KiB.")
    manifest = PluginManifest.model_validate_json(manifest_bytes)
    files = {"plugin.json": manifest_bytes}
    total_bytes = len(manifest_bytes)
    for relative in manifest.resources:
        resource_path(relative)
        path = root / relative
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(root)):
            raise ValueError("Symlink resources are not allowed.")
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError(f"Declared resource is missing or outside the plugin root: {relative}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"Declared resource exceeds 8 MiB: {relative}")
        with path.open("rb") as source:
            content = source.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(f"Declared resource exceeds 8 MiB: {relative}")
        total_bytes += len(content)
        if total_bytes > MAX_BUNDLE_BYTES:
            raise ValueError("Declared plugin content exceeds 32 MiB.")
        if b"PRIVATE KEY-----" in content:
            raise ValueError(f"Private key material is not a distributable resource: {relative}")
        files[relative] = content
    return manifest, files


def package(root: Path, output: Path) -> Path:
    manifest, files = inspect_bundle(root)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"CGA-Plugin-{manifest.id}-v{manifest.version}.zip"
    checksums = "".join(f"{hashlib.sha256(content).hexdigest()}  {name}\n"
                        for name, content in sorted(files.items()))
    files["SHA256SUMS.txt"] = checksums.encode("utf-8")
    with tempfile.NamedTemporaryFile(prefix=".cga-plugin-", dir=output, delete=False) as target:
        temporary = Path(target.name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        # Atomic, exclusive publication: never replace an existing release.
        os.link(temporary, destination)
    finally:
        temporary.unlink()
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("destination", type=Path)
    init.add_argument("--id", required=True)
    check = commands.add_parser("validate")
    check.add_argument("root", type=Path)
    bundle = commands.add_parser("package")
    bundle.add_argument("root", type=Path)
    bundle.add_argument("--output", type=Path, required=True)
    schema = commands.add_parser("schema")
    schema.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            print(scaffold(args.destination, args.id))
        elif args.command == "validate":
            manifest, files = inspect_bundle(args.root)
            print(json.dumps({"valid": True, "id": manifest.id, "version": manifest.version,
                              "files": len(files), "approved": False}))
        elif args.command == "package":
            print(package(args.root, args.output))
        else:
            with args.output.open("x", encoding="utf-8") as target:
                json.dump(PluginManifest.model_json_schema(), target, indent=2)
                target.write("\n")
    except ValidationError as error:
        print(json.dumps({"error": "Invalid plugin contract",
                          "issues": [{"location": list(item["loc"]), "type": item["type"]}
                                     for item in error.errors(include_input=False)]}), file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"Plugin operation failed: {error}", file=sys.stderr)
        return 2
    return 0
