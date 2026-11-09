from pathlib import Path
import re
import shutil
import subprocess

import pytest


SOURCE = Path(__file__).resolve().parents[1]


def _function(source: str, name: str) -> str:
    match = re.search(rf"(?:async )?function {name}\([^)]*\) \{{.*?^\}}", source, re.M | re.S)
    assert match is not None, name
    return match.group()


def _run(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute frontend JavaScript")
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True, timeout=10)


def test_authenticated_401_expires_session_once_without_logging_out_new_session() -> None:
    markup = (SOURCE / "frontend" / "index.html").read_text(encoding="utf-8")
    functions = "\n".join(_function(markup, name) for name in ("api", "expireSession"))
    _run(functions + """
const assert = require('node:assert/strict');
let _jwt = 'expired', logouts = 0, messages = 0;
const document = {getElementById: () => ({style:{}, set textContent(v) { messages++; }})};
function logout() { logouts++; _jwt = ''; }
let respond;
global.fetch = () => new Promise(resolve => { respond = resolve; });
async function fail(request, status=401) {
  respond({status, ok:false, statusText:'Unauthorized', json:async()=>({detail:'Not authenticated'})});
  await assert.rejects(request, /Not authenticated/);
}
(async () => {
  const first = api('GET', '/auth/projects');
  await fail(first);
  assert.equal(logouts, 1);
  assert.equal(messages, 1);
  await fail(api('GET', '/auth/projects'));
  assert.equal(logouts, 1);
  _jwt = 'old';
  const stale = api('GET', '/auth/projects');
  _jwt = 'new';
  await fail(stale);
  assert.equal(_jwt, 'new');
  assert.equal(logouts, 1);
  await fail(api('POST', '/auth/login', {}));
  assert.equal(logouts, 1);
  await fail(api('GET', '/auth/projects'), 403);
  assert.equal(_jwt, 'new');
})().catch(e => { console.error(e); process.exitCode=1; });
""")


def test_logout_stops_authenticated_polling_and_unloads_viewer() -> None:
    markup = (SOURCE / "frontend" / "index.html").read_text(encoding="utf-8")
    _run(_function(markup, "logout") + """
const assert = require('node:assert/strict');
let adcState={}, _jwt='expired', _me={};
let _indexStatusPollInterval=1, _auditPollInterval=2, _briefingPollInterval=3;
let _schedulePollInterval=4, _wsrAutoTimer=5, _msAdoPollTimer=6;
const stopped=[];
function clearInterval(id) { stopped.push(id); }
function clearTimeout(id) { stopped.push(id); }
const els=new Map();
const document={getElementById(id) {
  if (!els.has(id)) els.set(id,{style:{}, replaceChildren(){}, removeAttribute(name){this.removed=name;}});
  return els.get(id);
}};
const localStorage={removeItem(key){assert.equal(key,'cg_jwt');}};
function destroyLoginBackground() {}
function initLoginBackground() {}
logout();
assert.deepEqual(stopped,[1,2,3,4,5,6]);
assert.equal(_indexStatusPollInterval,null);
assert.equal(_schedulePollInterval,null);
assert.equal(_msAdoPollTimer,null);
assert.equal(els.get('graph-viewer-frame').removed,'src');
assert.equal(_jwt,'');
assert.equal(_me,null);
""")


def test_viewer_does_not_hide_authentication_failure_as_empty_project() -> None:
    source = (SOURCE / "viewer" / "main.js").read_text(encoding="utf-8")
    _run(_function(source, "projectWithGraphStats") + """
const assert = require('node:assert/strict');
let failure=Object.assign(new Error('Not authenticated'),{status:401});
async function fetchProjectStats() { throw failure; }
console.warn=()=>{};
(async () => {
  await assert.rejects(projectWithGraphStats({project_name:'example'}),/Not authenticated/);
  failure=Object.assign(new Error('Missing graph'),{status:404});
  const result=await projectWithGraphStats({project_name:'example'});
  assert.equal(result.viewerTotalNodes,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_navigation_cannot_restart_polling_after_session_expiry() -> None:
    markup = (SOURCE / "frontend" / "index.html").read_text(encoding="utf-8")
    _run(_function(markup, "switchTab") + """
let _jwt='', _me=null;
switchTab('briefing');
switchTab('schedule');
_jwt='token';
switchTab('audit');
""")
