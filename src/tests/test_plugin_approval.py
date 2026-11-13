import copy
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("plugin_approval", ROOT / "src/scripts/check_plugin_approval.py")
approval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(approval)
HEAD = "a" * 40
MERGE = "b" * 40
BASE = "c" * 40
WORKFLOW_ID = 42


def evidence():
    pr = {"number": 123, "head": {"sha": HEAD, "ref": "sdk", "repo": {"full_name": "nascousa/cga"}},
          "base": {"ref": "main", "sha": BASE, "repo": {"id": 1, "full_name": "nascousa/cga"}},
          "user": {"login": "author"}, "draft": False, "state": "closed", "merged": True,
          "merge_commit_sha": MERGE}
    reviews = [{"id": number, "state": "APPROVED", "commit_id": HEAD, "submitted_at": "2026-11-12",
                "user": {"login": login, "type": "User"}}
               for number, login in enumerate(("owner", "reviewer"), 1)]
    run = {"id": 10, "workflow_id": WORKFLOW_ID, "path": approval.TRUSTED_WORKFLOW,
           "event": "pull_request", "repository": {"full_name": "nascousa/cga"},
           "head_repository": {"full_name": "nascousa/cga"}, "head_sha": HEAD,
           "head_branch": "sdk", "check_suite_id": 20, "status": "completed", "conclusion": "success",
           "pull_requests": [{"number": 123, "head": {"sha": HEAD},
                              "base": {"ref": "main", "sha": BASE, "repo": {"id": 1}}}]}
    checks = [{"id": 1, "head_sha": HEAD, "app": {"slug": "github-actions"}, "_run": run,
               "check_suite": {"id": 20}, "_trusted_blob": "workflow-blob", "_tested_blob": "workflow-blob",
               "_head_blob": "workflow-blob",
               "name": "Plugin SDK tests", "status": "completed", "conclusion": "success"}]
    return pr, reviews, checks


def verify(pr, reviews, checks, **changes):
    options = dict(repository="nascousa/cga", head=HEAD, maintainers={"owner"},
                   security={"reviewer"}, required_checks={"Plugin SDK tests"}, release_commit=MERGE,
                   workflow_id=WORKFLOW_ID)
    return approval.verify(pr, reviews, checks, **{**options, **changes})


def test_exact_independent_approvals_authorize_only_the_merged_checkout():
    receipt = verify(*evidence())
    assert receipt["approved"] and receipt["head_sha"] == HEAD
    assert receipt["maintainer"] != receipt["security_reviewer"]


@pytest.mark.parametrize("case", ["stale", "author", "same-reviewer", "dismissed", "changes",
                                  "failed", "old-check", "missing-config", "wrong-merge", "draft", "bot"])
def test_release_fails_closed_for_invalid_or_changed_evidence(case):
    pr, reviews, checks = copy.deepcopy(evidence())
    options = {}
    if case == "stale":
        reviews[1]["commit_id"] = "c" * 40
    elif case == "author":
        pr["user"]["login"] = "reviewer"
    elif case == "same-reviewer":
        options = {"security": {"owner"}}
    elif case in {"dismissed", "changes"}:
        reviews.append({**reviews[1], "id": 3,
                        "state": "DISMISSED" if case == "dismissed" else "CHANGES_REQUESTED"})
    elif case == "failed":
        checks[0]["conclusion"] = "failure"
    elif case == "old-check":
        checks[0]["head_sha"] = "c" * 40
    elif case == "missing-config":
        options = {"security": set()}
    elif case == "wrong-merge":
        options = {"release_commit": "c" * 40}
    elif case == "draft":
        pr["draft"] = True
    elif case == "bot":
        reviews[1]["user"]["type"] = "Bot"
    with pytest.raises(ValueError):
        verify(pr, reviews, checks, **options)


def test_later_failed_check_cannot_be_hidden_by_an_earlier_success():
    pr, reviews, checks = evidence()
    checks.append({**checks[0], "id": 2, "conclusion": "cancelled"})
    with pytest.raises(ValueError, match="checks"):
        verify(pr, reviews, checks)


def test_synthetic_merge_check_proves_both_tested_head_and_base():
    pr, reviews, checks = evidence()
    checks[0]["head_sha"] = MERGE
    checks[0]["_tested_commit"] = {"parents": [{"sha": BASE}, {"sha": HEAD}]}
    assert verify(pr, reviews, checks)["approved"]
    checks[0]["_tested_commit"]["parents"][1]["sha"] = "d" * 40
    with pytest.raises(ValueError, match="checks"):
        verify(pr, reviews, checks)


def test_main_push_checks_authorize_the_exact_approved_merge_after_workflow_changes():
    pr, reviews, checks = evidence()
    checks[0].update(head_sha=MERGE, _tested_blob="changed-workflow")
    checks[0]["_run"].update(event="push", head_sha=MERGE, head_branch="main", pull_requests=[])
    assert verify(pr, reviews, checks)["approved"]
    checks[0]["_run"]["head_sha"] = HEAD
    with pytest.raises(ValueError, match="checks"):
        verify(pr, reviews, checks)


@pytest.mark.parametrize("case", ["workflow-id", "workflow-path", "event", "repository",
                                  "source", "branch", "suite", "association", "workflow-source",
                                  "head-workflow-source", "run-failed", "base", "run-head",
                                  "association-head", "base-repository"])
def test_matching_job_name_and_actions_app_do_not_authorize_untrusted_checks(case):
    pr, reviews, checks = evidence()
    check, run = checks[0], checks[0]["_run"]
    if case == "workflow-id":
        run["workflow_id"] += 1
    elif case == "workflow-path":
        run["path"] = ".github/workflows/untrusted.yml"
    elif case == "event":
        run["event"] = "pull_request_target"
    elif case == "repository":
        run["repository"]["full_name"] = "attacker/cga"
    elif case == "source":
        run["head_repository"]["full_name"] = "attacker/cga"
    elif case == "branch":
        run["head_branch"] = "unrelated"
    elif case == "suite":
        check["check_suite"]["id"] += 1
    elif case == "association":
        run["pull_requests"][0]["number"] += 1
    elif case == "workflow-source":
        check["_tested_blob"] = "attacker-workflow"
    elif case == "head-workflow-source":
        check["_head_blob"] = "attacker-workflow"
    elif case == "run-failed":
        run["conclusion"] = "failure"
    elif case == "base":
        check.update(head_sha=MERGE, _tested_commit={"parents": [{"sha": "d" * 40}, {"sha": HEAD}]})
    elif case == "run-head":
        run["head_sha"] = "d" * 40
    elif case == "association-head":
        run["pull_requests"][0]["head"]["sha"] = "d" * 40
    elif case == "base-repository":
        run["pull_requests"][0]["base"]["repo"]["id"] = 2
    with pytest.raises(ValueError, match="checks"):
        verify(pr, reviews, checks)


@pytest.mark.parametrize("edited_ref", [None, HEAD, MERGE])
def test_metadata_loader_reads_workflow_suites_and_immutable_workflow_blobs(monkeypatch, edited_ref):
    pr, _, checks = evidence()
    run = checks[0]["_run"]
    checks[0]["head_sha"] = MERGE
    requests = []

    def github(path, *, paginated=False):
        requests.append(path)
        if "/actions/workflows/plugin-sdk.yml" in path:
            return {"id": WORKFLOW_ID, "path": approval.TRUSTED_WORKFLOW, "state": "active"}
        if "/runs?" in path:
            return [{"workflow_runs": [run]}]
        if "/check-suites/" in path:
            return [{"check_runs": checks}]
        if "/contents/" in path:
            return {"sha": "attacker-workflow" if edited_ref and path.endswith(f"ref={edited_ref}")
                    else "workflow-blob"}
        if f"/commits/{MERGE}" in path:
            return {"parents": [{"sha": BASE}, {"sha": HEAD}]}
        raise AssertionError(path)

    monkeypatch.setattr(approval, "github", github)
    workflow_id, loaded = approval.workflow_checks("nascousa/cga", pr, HEAD, MERGE)
    assert workflow_id == WORKFLOW_ID
    if edited_ref:
        with pytest.raises(ValueError, match="checks"):
            verify(pr, evidence()[1], loaded)
    else:
        assert verify(pr, evidence()[1], loaded)["approved"]
    assert any("/check-suites/20/check-runs" in path for path in requests)
    assert not any(f"/commits/{HEAD}/check-runs" in path for path in requests)
    for ref in (BASE, HEAD, MERGE):
        assert f"repos/nascousa/cga/contents/{approval.TRUSTED_WORKFLOW}?ref={ref}" in requests


@pytest.mark.parametrize("error", [subprocess.CalledProcessError(1, ["gh", "api"]),
                                  subprocess.TimeoutExpired(["gh", "api"], 60),
                                  ValueError("Invalid metadata")])
def test_metadata_lookup_errors_cannot_produce_an_approval(monkeypatch, capsys, error):
    monkeypatch.setenv("CGA_PLUGIN_MAINTAINERS", "owner")
    monkeypatch.setenv("CGA_PLUGIN_SECURITY_REVIEWERS", "reviewer")

    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(approval, "github", unavailable)
    assert approval.main(["--pr", "123", "--head-sha", HEAD]) == 2
    assert not capsys.readouterr().out
