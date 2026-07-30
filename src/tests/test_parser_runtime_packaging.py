from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PARSER_REQUIREMENTS = (
    "tree-sitter==0.25.2",
    "tree-sitter-language-pack==0.13.0",
    "tree-sitter-c-sharp==0.23.1",
    "tree-sitter-embedded-template==0.25.0",
    "tree-sitter-yaml==0.7.2",
)


def test_dev_image_installs_pinned_parser_runtime() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile.dev").read_text(encoding="utf-8")

    for requirement in PARSER_REQUIREMENTS:
        assert requirement in requirements
        assert requirement in dockerfile


def test_compose_variants_persist_runtime_configuration() -> None:
    compose_files = (
        "docker-compose.yml",
        "docker-compose.desktop.yml",
        "docker-compose.release.yml",
        "deploy/docker-desktop/docker-compose.yml",
    )

    for relative_path in compose_files:
        compose = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "runtime_data:/app/data" in compose, relative_path
        assert "\n  runtime_data:" in compose, relative_path
