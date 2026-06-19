from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


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


