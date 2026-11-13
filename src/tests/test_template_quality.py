from pathlib import Path
import shutil
import subprocess
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def test_powershell_generator_copies_current_template_without_overwrite():
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is required to validate the template generator")
    target = ROOT / (".adc-generator-test-" + uuid.uuid4().hex)
    command = [shell, "-NoProfile", "-NonInteractive", "-File",
               str(ROOT / "src/scripts/generate-adc-template.ps1"), "-TargetDir", str(target)]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        source = ROOT / "adc-template"
        files = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        assert {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()} == files
        assert b"standards/conventions/research-reports.md" in (target / "index.md").read_bytes()
        assert (target / "standards/conventions/research-reports.md").is_file()
        (target / "index.md").write_bytes(b"Preserve existing template")
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert result.returncode != 0
        assert (target / "index.md").read_bytes() == b"Preserve existing template"
    finally:
        shutil.rmtree(target, ignore_errors=True)


def test_prompt_rules_has_core_quality_sections() -> None:
    content = _read("adc-template/prompt-rules.md")

    required_sections = [
        "## Mandatory Core Rules",
        "## Repository and Workflow Rules",
        "## ContextGraph Use Policy",
        "## ContextGraph Retrieval and Token Policy",
        "## ContextGraph-First Trigger Conditions",
        "## Allowed Exceptions",
    ]

    for section in required_sections:
        assert section in content


def test_security_convention_has_patch_and_update_strategies() -> None:
    content = _read("adc-template/standards/conventions/security.md")

    required_entries = [
        "## Version, Update, and Patch Security Strategies",
        "- **Security-First Versioning**",
        "- **Patch Cadence Policy**",
        "- **Time-Bounded Exceptions**",
        "- **Post-Patch Verification**",
    ]

    for entry in required_entries:
        assert entry in content


def test_testing_convention_has_quality_strategy_section() -> None:
    content = _read("adc-template/standards/conventions/testing.md")

    required_entries = [
        "## Common Test and Software Quality Strategies",
        "- **Branch Coverage Focus**",
        "- **Golden/Snapshot Validation**",
        "## Coverage Governance",
        "- **Minimum Baseline**: Maintain at least 80% line coverage",
    ]

    for entry in required_entries:
        assert entry in content


def test_devops_convention_has_cicd_github_webhook_policy() -> None:
    content = _read("adc-template/standards/conventions/devops.md")

    required_entries = [
        "## CI/CD Policy (GitHub + Webhook Deploy)",
        "CICD=enabled",
        "GITHUB_TOKEN",
        "https://github.com/nascousa/cga",
        "DEPLOY_WEBHOOK_URL",
        "main -> production",
        "Webhook-driven auto deploy",
    ]

    for entry in required_entries:
        assert entry in content


def test_cicd_preflight_gate_policy_is_explicit() -> None:
    runbook = _read("adc-template/standards/runbooks/002-cicd-github-webhook-debug.md")

    runbook_required_entries = [
        "Preflight Gate",
        "CICD=enabled",
        "GITHUB_TOKEN",
        "DEPLOY_WEBHOOK_URL",
        "Validate GitHub token",
        "Validate deployment webhook endpoint reachability",
    ]
    for entry in runbook_required_entries:
        assert entry in runbook


def test_all_powershell_scripts_live_under_src_scripts() -> None:
    root_ps1_files = list(ROOT.glob("*.ps1"))
    assert not root_ps1_files


def test_devops_convention_has_required_compose_healthcheck_block() -> None:
    content = _read("adc-template/standards/conventions/devops.md")

    required_entries = [
        "## Docker Compose Health Check Policy",
        "healthcheck:",
        "- CMD",
        "- curl",
        "- '-f'",
        "- 'http://localhost:8000/health'",
        "interval: 30s",
        "timeout: 10s",
        "retries: 3",
        "start_period: 40s",
    ]

    for entry in required_entries:
        assert entry in content


def test_falkordb_compose_volumes_mount_persistent_data_dir() -> None:
    compose_files = [
        "docker-compose.yml",
        "docker-compose.desktop.yml",
        "docker-compose.release.yml",
    ]

    for rel_path in compose_files:
        content = _read(rel_path)
        assert "falkordb_data:/data" not in content
        assert "falkordb_dev_data:/data" not in content
        assert ":/var/lib/falkordb/data" in content


