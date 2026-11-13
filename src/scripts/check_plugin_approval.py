"""Verify independent GitHub reviews and current checks; never trust author-supplied approvals."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

TRUSTED_WORKFLOW = ".github/workflows/plugin-sdk.yml"


def trusted_check(check: dict, pr: dict, *, repository: str, head: str,
                  release_commit: str | None, workflow_id: int) -> bool:
    run = check.get("_run", {})
    if (run.get("workflow_id") != workflow_id or run.get("path") != TRUSTED_WORKFLOW
            or run.get("repository", {}).get("full_name", "").lower() != repository.lower()
            or check.get("check_suite", {}).get("id") != run.get("check_suite_id")
            or check.get("app", {}).get("slug") != "github-actions"):
        return False
    source = run.get("head_repository", {}).get("full_name", "").lower()
    if run.get("event") == "push":
        return bool(release_commit and run.get("head_branch") == "main"
                    and source == repository.lower() and run.get("head_sha") == release_commit
                    and check.get("head_sha") == release_commit)
    if (run.get("event") != "pull_request" or run.get("head_sha") != head
            or source != pr["head"]["repo"]["full_name"].lower()
            or run.get("head_branch") != pr["head"]["ref"]
            or not check.get("_trusted_blob")
            or check.get("_head_blob") != check["_trusted_blob"]
            or check.get("_tested_blob") != check["_trusted_blob"]):
        return False
    associations = [item for item in run.get("pull_requests", [])
                    if item.get("number") == pr["number"] and item["head"]["sha"] == head
                    and item["base"]["ref"] == "main"
                    and item["base"]["repo"]["id"] == pr["base"]["repo"]["id"]]
    if not associations:
        return False
    if check.get("head_sha") == head:
        return True
    parents = [item["sha"] for item in check.get("_tested_commit", {}).get("parents", [])]
    return any(parents == [item["base"]["sha"], head] for item in associations)


def names(value: str) -> set[str]:
    result = {item.strip().lower() for item in value.split(",") if item.strip()}
    if not result or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,38}", item) for item in result):
        raise ValueError("Configure nonempty reviewer login lists; do not use display names or teams.")
    return result


def verify(pr: dict, reviews: list[dict], checks: list[dict], *, repository: str, head: str,
           maintainers: set[str], security: set[str], required_checks: set[str],
           release_commit: str | None = None, workflow_id: int | None = None) -> dict:
    if not maintainers or not security or not required_checks:
        raise ValueError("Maintainer, security and required-check configuration is mandatory.")
    if not re.fullmatch(r"[a-f0-9]{40}", head) or pr["head"]["sha"] != head:
        raise ValueError("Approval must match the exact current PR head commit.")
    if pr["base"]["repo"]["full_name"].lower() != repository.lower() or pr["base"]["ref"] != "main":
        raise ValueError("Only a PR targeting this repository's main branch can authorize publication.")
    if pr.get("draft") or (not pr.get("merged") and pr["state"] != "open"):
        raise ValueError("Draft or unmerged closed PRs cannot authorize publication.")
    if release_commit is not None and (not pr.get("merged") or pr.get("merge_commit_sha") != release_commit):
        raise ValueError("Release checkout must be the approved PR's merged commit.")
    author = pr["user"]["login"].lower()
    latest = {}
    for review in sorted(reviews, key=lambda item: item["id"]):
        login = review["user"]["login"].lower()
        if (review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
                and review.get("submitted_at") and review["user"].get("type") == "User"):
            latest[login] = review
    if any(item["state"] == "CHANGES_REQUESTED"
           for login, item in latest.items() if login in maintainers | security):
        raise ValueError("A trusted reviewer still requests changes.")
    approved = {login for login, item in latest.items()
                if login != author and item["state"] == "APPROVED" and item["commit_id"] == head}
    pairs = [(owner, reviewer) for owner in sorted(approved & maintainers)
             for reviewer in sorted(approved & security) if owner != reviewer]
    if not pairs:
        raise ValueError("Two distinct non-author human approvals are required: maintainer and security.")
    if not workflow_id:
        raise ValueError("Trusted workflow identity is required.")
    current_checks = {}
    for check in sorted(checks, key=lambda item: (item.get("_run", {}).get("id", 0), item["id"])):
        if trusted_check(check, pr, repository=repository, head=head,
                         release_commit=release_commit, workflow_id=workflow_id):
            current_checks[check["name"]] = check
    if any(name not in current_checks or current_checks[name].get("status") != "completed"
           or current_checks[name].get("conclusion") != "success"
           or current_checks[name]["_run"].get("status") != "completed"
           or current_checks[name]["_run"].get("conclusion") != "success"
           for name in required_checks):
        raise ValueError("Required checks must succeed in the trusted workflow on the approved source.")
    return {"repository": repository, "head_sha": head, "merge_commit_sha": release_commit,
            "maintainer": pairs[0][0], "security_reviewer": pairs[0][1],
            "checks": sorted(required_checks), "approved": True}


def github(path: str, *, paginated: bool = False):
    command = ["gh", "api", path]
    if paginated:
        command.extend(["--paginate", "--slurp"])
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
    return json.loads(result.stdout)


def workflow_checks(repository: str, pr: dict, head: str,
                    release_commit: str | None) -> tuple[int, list[dict]]:
    root = f"repos/{repository}"
    workflow = github(f"{root}/actions/workflows/plugin-sdk.yml")
    if workflow["path"] != TRUSTED_WORKFLOW or workflow["state"] != "active":
        raise ValueError("The trusted SDK workflow is unavailable.")
    runs = {}
    for sha in dict.fromkeys(filter(None, (head, release_commit))):
        for page in github(
                f"{root}/actions/workflows/{workflow['id']}/runs?head_sha={sha}&per_page=100",
                paginated=True):
            runs.update((run["id"], run) for run in page["workflow_runs"])
    checks = []
    blobs = {}

    def blob(ref: str) -> str:
        if ref not in blobs:
            blobs[ref] = github(f"{root}/contents/{TRUSTED_WORKFLOW}?ref={ref}")["sha"]
        return blobs[ref]

    for run in runs.values():
        if run.get("event") not in {"pull_request", "push"}:
            continue
        for page in github(f"{root}/check-suites/{run['check_suite_id']}/check-runs?per_page=100",
                           paginated=True):
            for check in page["check_runs"]:
                check["_run"] = run
                if run["event"] == "pull_request":
                    check["_trusted_blob"] = blob(pr["base"]["sha"])
                    check["_head_blob"] = blob(head)
                    check["_tested_blob"] = blob(check["head_sha"])
                    if check["head_sha"] != head:
                        check["_tested_commit"] = github(f"{root}/commits/{check['head_sha']}")
                checks.append(check)
    return workflow["id"], checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "nascousa/cga"))
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--release-commit")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository) or args.pr < 1:
            raise ValueError("A valid repository and positive PR number are required.")
        maintainers = names(os.environ.get("CGA_PLUGIN_MAINTAINERS", ""))
        security = names(os.environ.get("CGA_PLUGIN_SECURITY_REVIEWERS", ""))
        required = {name.strip() for name in os.environ.get(
            "CGA_PLUGIN_REQUIRED_CHECKS", "Plugin SDK tests").split(",") if name.strip()}
        pr = github(f"repos/{args.repository}/pulls/{args.pr}")
        reviews = [review for page in github(
            f"repos/{args.repository}/pulls/{args.pr}/reviews?per_page=100", paginated=True) for review in page]
        workflow_id, checks = workflow_checks(args.repository, pr, args.head_sha, args.release_commit)
        receipt = verify(pr, reviews, checks, repository=args.repository, head=args.head_sha,
                         maintainers=maintainers, security=security, required_checks=required,
                         release_commit=args.release_commit, workflow_id=workflow_id)
        print(json.dumps({**receipt, "pr": args.pr}))
    except (ValueError, KeyError) as error:
        print(f"Plugin approval rejected: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError:
        print("Plugin approval rejected: GitHub metadata request failed.", file=sys.stderr)
        return 2
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"Plugin approval unavailable: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
