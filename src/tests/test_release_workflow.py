import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_relay_checksums_use_flat_release_asset_names() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "action-gh-release uploads each configured path using its basename" in workflow
    assert "cd relay-dist" in workflow
    assert 'sha256sum cga-relay.exe "cga-relay-${version}-windows-x64.zip"' in workflow
    assert "relay-dist/*" in workflow


def test_source_archive_excludes_old_generated_release_binaries() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "/dist export-ignore" in attributes


def test_public_image_tags_are_promoted_only_after_all_release_gates() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    publish = workflow["jobs"]["publish"]
    assert set(publish["needs"]) == {"relay-windows", "relay-unix"}
    assert "if" not in publish
    assert "continue-on-error" not in publish
    for dependency in publish["needs"]:
        job = workflow["jobs"][dependency]
        assert "if" not in job
        assert "continue-on-error" not in job
        assert all(not step.get("continue-on-error") for step in job["steps"])
    windows = workflow["jobs"]["relay-windows"]["steps"]
    assert any("-RequireSignature" in step.get("run", "") for step in windows)
    unix = workflow["jobs"]["relay-unix"]
    assert set(unix["strategy"]["matrix"]["os"]) == {
        "ubuntu-24.04", "ubuntu-24.04-arm", "macos-15", "macos-15-intel"}
    for name in ("Build signed Linux release", "Build signed and notarized macOS CLI release"):
        platform = next(step for step in unix["steps"] if step.get("name") == name)
        assert "build-unix-package.sh --release" in platform["run"]
        assert ":?" in platform["run"]
    steps = publish["steps"]
    build = next(step for step in steps if step.get("id") == "api-image")
    assert "tags" not in build["with"]
    assert "push-by-digest=true" in build["with"]["outputs"]
    sign_index = next(i for i, step in enumerate(steps) if "Sign API update manifest" in step.get("name", ""))
    assets_index = next(i for i, step in enumerate(steps) if step.get("name") == "Create GitHub release")
    promote_index = next(i for i, step in enumerate(steps) if step.get("name", "").startswith("Promote verified"))
    assert sign_index < assets_index < promote_index == len(steps) - 1
    assert "${CGA_UPDATE_SIGNING_KEY:?" in steps[sign_index]["run"]
    assert "${{ steps.api-image.outputs.digest }}" in steps[sign_index]["run"]
    promote = steps[promote_index]
    assert promote["env"]["API_DIGEST"] == "${{ steps.api-image.outputs.digest }}"
    assert '"$API_IMAGE@$API_DIGEST"' in promote["run"]
    assert "--prefer-index=false" in promote["run"]
    assert '--tag "$API_IMAGE:$GITHUB_REF_NAME"' in promote["run"]
    assert '--tag "$API_IMAGE:latest"' in promote["run"]
    assert "if" not in promote
    assert "continue-on-error" not in promote
    assert all(not step.get("continue-on-error") for step in steps)
    assert "if" not in steps[sign_index]
    assert "if" not in steps[assets_index]


def bash_executable() -> str:
    if os.name == "nt":
        git = shutil.which("git")
        candidate = Path(git).parents[1] / "bin/bash.exe" if git else None
        if candidate and candidate.exists():
            return str(candidate)
    elif shutil.which("bash"):
        return shutil.which("bash")
    pytest.skip("Bash is required to exercise release publication guards")


@pytest.mark.parametrize("case", ["new", "tag-only", "existing-release", "tag-error",
                                  "release-error", "malformed-count", "tag-race"])
def test_sdk_publication_refuses_existing_tags_and_lookup_failures(case: str) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/plugin-sdk-release.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["publish"]["steps"]
    script = next(step["run"] for step in steps
                  if step.get("name") == "Publish new immutable developer-tool release")
    mock = r'''
gh() {
  case "$*" in
    *matching-refs*)
      [[ "$CASE" != "tag-error" ]] || return 1
      if [[ "$CASE" == "tag-only" ]]; then echo 1
      elif [[ "$CASE" == "malformed-count" ]]; then echo invalid
      else echo 0; fi ;;
    *releases?*)
      [[ "$CASE" != "release-error" ]] || return 1
      if [[ "$CASE" == "existing-release" ]]; then echo 1; else echo 0; fi ;;
    *"POST "*"git/refs"*)
      [[ "$CASE" != "tag-race" ]] || return 1
      echo TAG_CREATED ;;
    "release create "*)
      [[ "$*" == *"--verify-tag"* && "$*" != *"--target"* ]] || return 1
      echo RELEASE_CREATED ;;
    *) echo "Unexpected gh invocation: $*" >&2; return 1 ;;
  esac
}
'''
    result = subprocess.run([bash_executable(), "-c", mock + script], capture_output=True,
                            text=True, timeout=30, cwd=ROOT,
                            env={**os.environ, "CASE": case, "SDK_VERSION": "1.0.0",
                                 "APPROVED_COMMIT": "a" * 40, "GITHUB_REPOSITORY": "nascousa/cga",
                                 "RUNNER_TEMP": str(ROOT / "dist")})
    if case == "new":
        assert result.returncode == 0, result.stderr
        assert result.stdout.index("TAG_CREATED") < result.stdout.index("RELEASE_CREATED")
    else:
        assert result.returncode != 0
        assert "RELEASE_CREATED" not in result.stdout
        assert "TAG_CREATED" not in result.stdout