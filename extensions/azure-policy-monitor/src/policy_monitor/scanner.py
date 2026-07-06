"""Deterministic scanner for Azure Policy repository parity checks."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

GUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
RISKY_EFFECTS = {"deny", "deployifnotexists", "modify", "append"}
SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass(frozen=True)
class PolicyMonitorConfig:
    repo_path: str
    policy_root: str = "settings/BuiltInPoliciesV2"
    cloud_folders: list[str] = field(default_factory=lambda: ["AllEnvironments", "USNat", "USSec"])
    baseline_folder: str = "AllEnvironments"


@dataclass(frozen=True)
class PolicyFileInfo:
    folder: str
    relative_path: str
    path: str
    guid: str | None
    version: str | None
    effects: list[str]


@dataclass(frozen=True)
class PolicyFinding:
    check: str
    severity: str
    message: str
    file: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


def scan_policy_repository(config: PolicyMonitorConfig) -> dict[str, Any]:
    repo_path = Path(config.repo_path).expanduser().resolve()
    policy_root = _resolve_policy_root(repo_path, config.policy_root)
    findings: list[PolicyFinding] = []

    if not repo_path.exists():
        findings.append(
            PolicyFinding(
                check="repo_path",
                severity="critical",
                message="Repository path does not exist.",
                evidence={"repo_path": str(repo_path)},
            )
        )
        return _build_result(config, repo_path, policy_root, {}, findings)

    if not policy_root.exists():
        findings.append(
            PolicyFinding(
                check="policy_root",
                severity="critical",
                message="Policy root does not exist.",
                evidence={"policy_root": str(policy_root)},
            )
        )
        return _build_result(config, repo_path, policy_root, {}, findings)

    folder_files = _collect_policy_files(policy_root, config.cloud_folders, findings)
    baseline = folder_files.get(config.baseline_folder) or {}
    if not baseline:
        findings.append(
            PolicyFinding(
                check="baseline_folder",
                severity="high",
                message="Baseline policy folder has no JSON policy files.",
                evidence={"baseline_folder": config.baseline_folder},
            )
        )

    _check_folder_parity(config, folder_files, findings)
    _check_guid_consistency(config, folder_files, findings)
    _check_version_consistency(config, folder_files, findings)
    _check_risky_effects(folder_files, findings)
    return _build_result(config, repo_path, policy_root, folder_files, findings)


def _resolve_policy_root(repo_path: Path, policy_root: str) -> Path:
    candidate = Path(policy_root).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (repo_path / candidate).resolve()


def _collect_policy_files(
    policy_root: Path,
    cloud_folders: list[str],
    findings: list[PolicyFinding],
) -> dict[str, dict[str, PolicyFileInfo]]:
    folder_files: dict[str, dict[str, PolicyFileInfo]] = {}
    for folder in cloud_folders:
        folder_path = policy_root / folder
        folder_files[folder] = {}
        if not folder_path.exists():
            findings.append(
                PolicyFinding(
                    check="cloud_folder",
                    severity="high",
                    message="Configured cloud folder is missing.",
                    evidence={"folder": folder, "path": str(folder_path)},
                )
            )
            continue
        for path in sorted(folder_path.rglob("*.json")):
            relative_path = path.relative_to(folder_path).as_posix()
            folder_files[folder][relative_path] = _parse_policy_file(folder, relative_path, path, findings)
    return folder_files


def _parse_policy_file(
    folder: str,
    relative_path: str,
    path: Path,
    findings: list[PolicyFinding],
) -> PolicyFileInfo:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        findings.append(
            PolicyFinding(
                check="json_parse",
                severity="critical",
                message="Policy file is not valid JSON.",
                file=f"{folder}/{relative_path}",
                evidence={"error": str(exc)},
            )
        )
        return PolicyFileInfo(folder, relative_path, str(path), None, None, [])

    return PolicyFileInfo(
        folder=folder,
        relative_path=relative_path,
        path=str(path),
        guid=_extract_guid(data, relative_path),
        version=_extract_version(data),
        effects=sorted(_extract_effects(data)),
    )


def _extract_guid(data: Any, relative_path: str) -> str | None:
    candidates: list[str] = []
    if isinstance(data, dict):
        for key in ("name", "id"):
            value = data.get(key)
            if isinstance(value, str):
                candidates.append(value)
        properties = data.get("properties")
        if isinstance(properties, dict):
            for key in ("name", "id"):
                value = properties.get(key)
                if isinstance(value, str):
                    candidates.append(value)
    candidates.append(relative_path)
    for candidate in candidates:
        match = GUID_PATTERN.search(candidate)
        if match:
            return match.group(0).lower()
    return None


def _extract_version(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    metadata = data.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("version"), str):
        return metadata["version"]
    properties = data.get("properties")
    if isinstance(properties, dict):
        metadata = properties.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("version"), str):
            return metadata["version"]
    return None


def _extract_effects(data: Any) -> set[str]:
    effects: set[str] = set()
    if isinstance(data, dict):
        for key, value in data.items():
            if key.lower() == "effect" and isinstance(value, str):
                effects.add(value)
            else:
                effects.update(_extract_effects(value))
    elif isinstance(data, list):
        for item in data:
            effects.update(_extract_effects(item))
    return effects


def _check_folder_parity(
    config: PolicyMonitorConfig,
    folder_files: dict[str, dict[str, PolicyFileInfo]],
    findings: list[PolicyFinding],
) -> None:
    baseline = folder_files.get(config.baseline_folder) or {}
    for relative_path in sorted(baseline):
        for folder in config.cloud_folders:
            if folder == config.baseline_folder:
                continue
            if relative_path not in folder_files.get(folder, {}):
                findings.append(
                    PolicyFinding(
                        check="folder_parity",
                        severity="high" if folder in {"USNat", "USSec"} else "medium",
                        message="Policy file is missing from a configured cloud folder.",
                        file=relative_path,
                        evidence={"missing_folder": folder, "baseline_folder": config.baseline_folder},
                    )
                )


def _check_guid_consistency(
    config: PolicyMonitorConfig,
    folder_files: dict[str, dict[str, PolicyFileInfo]],
    findings: list[PolicyFinding],
) -> None:
    relative_paths = sorted({path for files in folder_files.values() for path in files})
    for relative_path in relative_paths:
        observed = {
            folder: files[relative_path].guid
            for folder, files in folder_files.items()
            if relative_path in files and files[relative_path].guid
        }
        if len(set(observed.values())) > 1:
            findings.append(
                PolicyFinding(
                    check="guid_consistency",
                    severity="critical",
                    message="Policy GUID differs across cloud folders.",
                    file=relative_path,
                    evidence={"observed": observed},
                )
            )
        for folder in config.cloud_folders:
            info = folder_files.get(folder, {}).get(relative_path)
            if info and not info.guid:
                findings.append(
                    PolicyFinding(
                        check="guid_present",
                        severity="medium",
                        message="Policy GUID could not be found in file name, name, or id.",
                        file=f"{folder}/{relative_path}",
                    )
                )


def _check_version_consistency(
    config: PolicyMonitorConfig,
    folder_files: dict[str, dict[str, PolicyFileInfo]],
    findings: list[PolicyFinding],
) -> None:
    relative_paths = sorted({path for files in folder_files.values() for path in files})
    sovereign_folders = [folder for folder in config.cloud_folders if folder.lower() in {"usnat", "ussec"}]
    for relative_path in relative_paths:
        baseline_version = folder_files.get(config.baseline_folder, {}).get(relative_path)
        for folder in sovereign_folders:
            info = folder_files.get(folder, {}).get(relative_path)
            if not info:
                continue
            if not info.version:
                findings.append(
                    PolicyFinding(
                        check="version_present",
                        severity="medium",
                        message="Sovereign cloud policy file is missing metadata version.",
                        file=f"{folder}/{relative_path}",
                    )
                )
            if baseline_version and baseline_version.version and info.version and info.version != baseline_version.version:
                findings.append(
                    PolicyFinding(
                        check="version_consistency",
                        severity="high",
                        message="Sovereign cloud policy version differs from baseline version.",
                        file=relative_path,
                        evidence={
                            "baseline_folder": config.baseline_folder,
                            "baseline_version": baseline_version.version,
                            "folder": folder,
                            "version": info.version,
                        },
                    )
                )


def _check_risky_effects(
    folder_files: dict[str, dict[str, PolicyFileInfo]],
    findings: list[PolicyFinding],
) -> None:
    for folder, files in folder_files.items():
        for relative_path, info in files.items():
            risky = sorted(effect for effect in info.effects if effect.lower() in RISKY_EFFECTS)
            if risky:
                findings.append(
                    PolicyFinding(
                        check="effect_risk",
                        severity="medium" if folder not in {"USNat", "USSec"} else "high",
                        message="Policy uses an effect that requires explicit rollout review.",
                        file=f"{folder}/{relative_path}",
                        evidence={"effects": risky},
                    )
                )


def _build_result(
    config: PolicyMonitorConfig,
    repo_path: Path,
    policy_root: Path,
    folder_files: dict[str, dict[str, PolicyFileInfo]],
    findings: list[PolicyFinding],
) -> dict[str, Any]:
    finding_dicts = [asdict(finding) for finding in findings]
    worst = _worst_severity(finding.severity for finding in findings)
    folder_counts = {folder: len(files) for folder, files in folder_files.items()}
    return {
        "extension_id": "azure_policy_change_monitor",
        "status": "completed",
        "severity": worst,
        "summary": {
            "repo_path": str(repo_path),
            "policy_root": str(policy_root),
            "cloud_folders": list(config.cloud_folders),
            "baseline_folder": config.baseline_folder,
            "folder_counts": folder_counts,
            "finding_count": len(findings),
            "severity_counts": _severity_counts(findings),
        },
        "findings": finding_dicts,
    }


def _worst_severity(severities: Any) -> str:
    worst = "info"
    for severity in severities:
        if SEVERITY_ORDER.get(str(severity), 0) > SEVERITY_ORDER[worst]:
            worst = str(severity)
    return worst


def _severity_counts(findings: list[PolicyFinding]) -> dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts
