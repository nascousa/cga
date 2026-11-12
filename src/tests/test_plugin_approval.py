import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("plugin_approval", ROOT / "src/scripts/check_plugin_approval.py")
approval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(approval)
HEAD = "a" * 40
MERGE = "b" * 40


def evidence():
    pr = {"head": {"sha": HEAD}, "base": {"ref": "main", "repo": {"full_name": "nascousa/cga"}},
          "user": {"login": "author"}, "draft": False, "state": "closed", "merged": True,
          "merge_commit_sha": MERGE}
    reviews = [{"id": number, "state": "APPROVED", "commit_id": HEAD, "submitted_at": "2026-11-12",
                "user": {"login": login, "type": "User"}}
               for number, login in enumerate(("owner", "reviewer"), 1)]
    checks = [{"id": 1, "head_sha": HEAD, "app": {"slug": "github-actions"},
               "name": "Plugin SDK tests", "status": "completed", "conclusion": "success"}]
    return pr, reviews, checks


def verify(pr, reviews, checks, **changes):
    options = dict(repository="nascousa/cga", head=HEAD, maintainers={"owner"},
                   security={"reviewer"}, required_checks={"Plugin SDK tests"}, release_commit=MERGE)
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
