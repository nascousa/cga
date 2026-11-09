import re
import shutil
import subprocess
from pathlib import Path

import pytest


FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


def test_query_policy_ui_load_save_failures_and_diagnostics():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute frontend JavaScript")
    html = FRONTEND.read_text(encoding="utf-8")
    names = (
        "setQueryPolicyAvailability", "renderQueryPolicySettings",
        "saveQueryPolicySettings", "loadQueryDecisions", "loadSettings", "esc",
    )
    functions = []
    for name in names:
        match = re.search(
            rf"(?:async )?function {name}\([^)]*\) \{{.*?^\}}", html, re.M | re.S
        )
        assert match is not None, name
        functions.append(match.group())
    script = "\n".join(functions) + r"""
const assert = require('node:assert/strict');
let _queryPolicyReady=false, _runtimeConfig=null, _me={role:'admin',username:'test'}, _falkordbUrl='';
const nodes = new Map(), messages = [];
const document = {getElementById(id) {
  if (!nodes.has(id)) nodes.set(id, {value:'', disabled:true, textContent:'', innerHTML:'',
    replaceChildren() { this.innerHTML=''; }});
  return nodes.get(id);
}};
const field = name => document.getElementById('settings-query-' + name);
const toast = (message, kind) => messages.push({message,kind});
const renderIndexingSettings = () => {}, renderIndexingSummary = () => {};
let cfg={query_policy:{mode:'auto',max_response_bytes:4096,evidence_detail:'compact'}};
let api=async(method,path,body)=>{
  assert.equal(method,'GET'); assert.equal(path,'/admin/runtime-config');
  assert.equal(field('save').disabled,true);
  return structuredClone(cfg);
};
(async()=>{
  await loadSettings();
  assert.equal(_queryPolicyReady,true);
  assert.equal(field('mode').value,'auto');
  assert.equal(field('save').disabled,false);
  assert.match(field('status').textContent,/Active: auto/);
  field('budget').value='2047';
  await saveQueryPolicySettings();
  assert.equal(messages.at(-1).kind,'error');
  field('budget').value='2048';
  field('mode').value='evidence_first';
  api=async(method,path,body)=>{
    assert.equal(method,'PATCH'); assert.equal(path,'/admin/runtime-config');
    assert.equal(field('save').disabled,true);
    assert.deepEqual(body,{query_policy:{mode:'evidence_first',max_response_bytes:2048,evidence_detail:'compact'}});
    cfg=body; return structuredClone(cfg);
  };
  await saveQueryPolicySettings();
  assert.match(field('status').textContent,/Active: evidence_first/);
  assert.equal(_runtimeConfig.query_policy.max_response_bytes,2048);
  api=async()=>{throw new Error('Forbidden');};
  field('mode').value='auto';
  await saveQueryPolicySettings();
  assert.match(field('status').textContent,/Save failed/);
  assert.equal(_runtimeConfig.query_policy.mode,'evidence_first');
  assert.equal(field('save').disabled,false);
  await loadSettings();
  assert.equal(_queryPolicyReady,false);
  assert.equal(field('save').disabled,true);
  assert.equal(field('mode').disabled,true);
  assert.match(field('status').textContent,/Unable to load/);
  renderQueryPolicySettings({});
  assert.equal(_queryPolicyReady,false);
  renderQueryPolicySettings(cfg);
  api=async(method,path)=>{
    assert.equal(path,'/admin/query-routing/recent');
    return {records:[{timestamp:'now',project_id:'<script>alert(1)</script>',relation:'CALLS',
      intent:'lookup',mode:'auto',selected_tool:'find_callees',reason:'auto_simple_lookup',
      cache_status:'hit',duration_ms:1.2,response_bytes:1000,budget_bytes:2048,returned_items:2}]};
  };
  await loadQueryDecisions();
  assert.match(field('decisions').innerHTML,/&lt;script&gt;/);
  assert.doesNotMatch(field('decisions').innerHTML,/<script>/);
  assert.match(field('decisions-status').textContent,/1 successful/);
  api=async()=>{throw new Error('Backend unavailable');};
  await loadQueryDecisions();
  assert.equal(field('decisions').innerHTML,'');
  assert.match(field('decisions-status').textContent,/Unable to load/);
  assert.equal(field('refresh').disabled,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True, timeout=15)
