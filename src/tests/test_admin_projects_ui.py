from pathlib import Path
import re
import shutil
import subprocess

import pytest


FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


def test_project_incremental_index_action_uses_explicit_label() -> None:
    markup = FRONTEND.read_text(encoding="utf-8")

    assert ">Incremental Index</button>" in markup
    assert "btn.textContent = 'Incremental Index'" in markup
    assert ">Reindex</button>" not in markup


def test_project_incremental_index_action_sends_local_relay_intent() -> None:
    markup = FRONTEND.read_text(encoding="utf-8")

    assert "'X-CGA-Relay-Intent': 'index-git-incremental'" in markup


def test_retrying_jobs_remain_active_in_project_controls() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the existing frontend's JavaScript")
    markup = FRONTEND.read_text(encoding="utf-8")
    functions = []
    for name in ("isIndexJobActive", "getIndexTrackerState"):
        match = re.search(rf"function {name}\(job\) \{{.*?^\}}", markup, re.M | re.S)
        assert match is not None
        functions.append(match.group())
    script = "\n".join(functions) + """
const assert = require('node:assert/strict');
for (const status of ['pending', 'processing', 'retrying'])
  assert.equal(isIndexJobActive({status}), true);
for (const status of ['done', 'failed', 'stale'])
  assert.equal(isIndexJobActive({status}), false);
assert.equal(isIndexJobActive({status: 'retrying', is_stale: true}), false);
assert.equal(getIndexTrackerState({status: 'retrying'}), 'pending');
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True, timeout=10)