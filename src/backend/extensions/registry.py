"""Registry and adapters for CGA extensions."""
from __future__ import annotations

import sys
import re
from pathlib import Path
from typing import Any

from backend.extensions.models import ExtensionDefinition

AZURE_POLICY_EXTENSION_ID = "azure_policy_change_monitor"
WINDOWS_REPOS_PATH = re.compile(r"^[A-Za-z]:[\\/]+Repos(?:[\\/]+(?P<tail>.*))?$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _azure_policy_src_path() -> Path:
    return _repo_root() / "extensions" / "azure-policy-monitor" / "src"


def _ensure_extension_path(path: Path) -> None:
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)


def _normalize_repo_path(repo_path: str, repos_mount: Path = Path("/repos")) -> str:
    text = str(repo_path or "").strip()
    match = WINDOWS_REPOS_PATH.match(text)
    if not match or not repos_mount.exists():
        return text
    mapped = repos_mount
    tail = (match.group("tail") or "").replace("\\", "/").strip("/")
    for part in tail.split("/"):
        if part:
            mapped = mapped / part
    return str(mapped)


EXTENSIONS: dict[str, ExtensionDefinition] = {
    AZURE_POLICY_EXTENSION_ID: ExtensionDefinition(
        extension_id=AZURE_POLICY_EXTENSION_ID,
        name="Azure Policy Change Monitor",
        description="Scans Azure Policy repositories for sovereign cloud parity, GUID/version consistency, and risky effects.",
        version="0.1.0",
        capabilities=[
            "policy_repo_scan",
            "folder_parity",
            "guid_consistency",
            "version_consistency",
            "effect_risk",
        ],
        default_config={
            "policy_root": "settings/BuiltInPoliciesV2",
            "cloud_folders": ["AllEnvironments", "USNat", "USSec"],
            "baseline_folder": "AllEnvironments",
            "read_only": True,
        },
    )
}


def list_extension_definitions() -> list[ExtensionDefinition]:
    return list(EXTENSIONS.values())


def get_extension_definition(extension_id: str) -> ExtensionDefinition | None:
    return EXTENSIONS.get(extension_id)


def run_extension(extension_id: str, config: dict[str, Any]) -> dict[str, Any]:
    if extension_id != AZURE_POLICY_EXTENSION_ID:
        raise KeyError(f"Unknown extension: {extension_id}")
    _ensure_extension_path(_azure_policy_src_path())
    from policy_monitor import PolicyMonitorConfig, scan_policy_repository

    monitor_config = PolicyMonitorConfig(
        repo_path=_normalize_repo_path(str(config.get("repo_path") or "")),
        policy_root=str(config.get("policy_root") or "settings/BuiltInPoliciesV2"),
        cloud_folders=list(config.get("cloud_folders") or ["AllEnvironments", "USNat", "USSec"]),
        baseline_folder=str(config.get("baseline_folder") or "AllEnvironments"),
    )
    return scan_policy_repository(monitor_config)
