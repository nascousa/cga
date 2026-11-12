import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from pydantic import ValidationError

from cga_plugin_sdk import ExecutionResult, PluginManifest
from cga_plugin_sdk import cli
from cga_plugin_sdk.cli import inspect_bundle, main, package, scaffold


@pytest.fixture
def plugin(tmp_path):
    return scaffold(tmp_path / "plugin", "sample_inspector")


@pytest.mark.parametrize("change", [
    {"id": "audit"}, {"id": "memory"}, {"approved": True}, {"timeout_seconds": 0}, {"schema_version": True},
    {"permissions": ["network.outbound"]}, {"secret_env": ["secret-value"]},
    {"resources": ["plugin.py", "../secret"]}, {"resources": ["plugin.py", "CON.txt"]},
    {"resources": ["plugin.py", "README.md", "readme.md"]},
    {"resources": ["plugin.py", "file\nname"]},
    {"network_hosts": ["a.-b.example"], "permissions": ["network.outbound"]},
    {"version": "1\u0662.0.0"},
    {"resources": ["plugin.py", "LICENSE", "PLUGIN.JSON"]},
    {"resources": ["plugin.py", "LICENSE", "sha256sums.TXT"]},
])
def test_manifest_rejects_unsafe_or_unreviewable_contract(plugin, change):
    data = json.loads((plugin / "plugin.json").read_text())
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({**data, **change})


def test_static_validation_does_not_import_code_and_packaging_is_deterministic(plugin, tmp_path):
    (plugin / "plugin.py").write_text("raise RuntimeError('must never import during validation')", encoding="utf-8")
    (plugin / "not-declared.txt").write_text("not distributable", encoding="utf-8")
    manifest, files = inspect_bundle(plugin)
    assert manifest.id == "sample_inspector" and "not-declared.txt" not in files
    first = package(plugin, tmp_path / "first")
    second = package(plugin, tmp_path / "second")
    assert first.read_bytes() == second.read_bytes()
    original = first.read_bytes()
    with pytest.raises(FileExistsError):
        package(plugin, first.parent)
    assert first.read_bytes() == original
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist()) == {*files, "SHA256SUMS.txt"}
        for line in archive.read("SHA256SUMS.txt").decode().splitlines():
            digest, name = line.split("  ", 1)
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest


def test_example_matches_host_tuple_and_requires_project(plugin):
    spec = importlib.util.spec_from_file_location("generated_plugin", plugin / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert asyncio.run(module.execute(SimpleNamespace(project_external_id=None), "", 0))[0] == "failed"
    result = asyncio.run(module.execute(SimpleNamespace(project_external_id="P1"), "", 0))
    assert result == ("success", 200, {"project_id": "P1",
                                     "summary": "Read-only example; no external action was performed."}, "")


def test_total_size_limit_stops_before_reading_later_resources(plugin, monkeypatch):
    limit = (plugin / "plugin.json").stat().st_size + (plugin / "plugin.py").stat().st_size - 1
    monkeypatch.setattr(cli, "MAX_BUNDLE_BYTES", limit)
    original_open = Path.open

    def checked_open(path, *args, **kwargs):
        if path.name == "test_plugin.py":
            pytest.fail("Validation read another resource after exceeding its total byte budget.")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    with pytest.raises(ValueError, match="32 MiB"):
        inspect_bundle(plugin)


def test_missing_resource_and_key_material_are_explicit_failures(plugin, capsys):
    (plugin / "plugin.py").write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    assert main(["validate", str(plugin)]) == 2
    assert "Private key material" in capsys.readouterr().err
    (plugin / "plugin.py").unlink()
    assert main(["validate", str(plugin)]) == 2
    assert "missing" in capsys.readouterr().err


@pytest.mark.parametrize("result", [
    ExecutionResult("success", {}, response_status=500),
    ExecutionResult("failed", {}),
    ExecutionResult("success", {"too_large": "x" * 65536}),
    ExecutionResult("success", {"invalid": float("nan")}),
])
def test_result_never_hides_failure_or_unbounded_output(result):
    with pytest.raises(ValueError):
        result.to_host_result()


def test_scaffold_does_not_replace_existing_directory(plugin):
    with pytest.raises(FileExistsError):
        scaffold(plugin, "another_plugin")


def test_exported_schema_is_versioned_and_never_overwritten(tmp_path):
    schema = tmp_path / "plugin.schema.json"
    assert main(["schema", "--output", str(schema)]) == 0
    data = json.loads(schema.read_text(encoding="utf-8"))
    assert data["$id"] == "urn:cga:plugin:manifest:1" and data["additionalProperties"] is False
    original = schema.read_bytes()
    assert main(["schema", "--output", str(schema)]) == 2
    assert schema.read_bytes() == original


def test_symlink_resource_is_rejected(plugin, tmp_path):
    original = plugin / "plugin.py"
    content = tmp_path / "source.py"
    original.replace(content)
    try:
        original.symlink_to(content)
    except OSError as error:
        pytest.skip(f"Symlink creation unavailable: {error}")
    with pytest.raises(ValueError, match="Symlink"):
        inspect_bundle(plugin)
