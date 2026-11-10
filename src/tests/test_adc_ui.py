import shutil
import subprocess
from pathlib import Path

import pytest


def test_adc_tabs_versions_chart_and_collection_failures():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute frontend JavaScript")
    root = Path(__file__).resolve().parents[1] / "frontend"
    setup = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.value=''; this.style={}; this.attributes={}; this.listeners={}; this.files=[]; }
  append(child) { this.children.push(child); if (this.tag==='select' && !this.value && child.value) this.value=child.value; }
  replaceChildren(...children) { this.children=children; }
  insertBefore(child) { this.children.unshift(child); }
  setAttribute(name,value) { this.attributes[name]=value; }
  addEventListener(name,fn) { this.listeners[name]=fn; }
  focus() {}
  remove() {}
}
const manager = new Element();
const document = {createElement:tag=>new Element(tag), getElementById:()=>manager,
  documentElement:new Element(), body:new Element(), head:new Element()};
let option, disposed=false, disconnected=0;
const chart={on(){},resize(){},clear(){},dispose(){disposed=true;},setOption(value){option=value;}};
const window = {echarts:{init:()=>chart}};
const ResizeObserver = class {observe(){} disconnect(){disconnected++;}};
const MutationObserver = class {observe(){} disconnect(){disconnected++;}};
const getComputedStyle=()=>({getPropertyValue:name=>name==='--text'?'#ddd':'#111'});
const _me={role:'admin'}, _jwt='test-only';
const toast=()=>{}, confirm=()=>true, prompt=()=> 'Reviewed centralized rules';
const release={id:1,version:'1.0.0',major:1,minor:0,patch:0,documents:{'.adc/index.md':'Master'},
  source:'test',actor:'admin',created_at:'now',reason:'initial',sha256:'hash'};
const inventory={releases:[release],projects:[{id:7,project_name:'Alpha <script>'},{id:8,project_name:'Unbound'}],
  revisions:[{project_id:7,revision:1,release_id:1,actor:'admin',created_at:'now',reason:'Adopt'},
    {project_id:7,revision:2,release_id:1,actor:'admin',created_at:'now',reason:'Restore',restored_from:1}]};
let api=async(method,path)=>{
  if(path==='/adc/releases') return [release];
  if(path==='/adc/releases/1') return release;
  if(path==='/auth/projects') return inventory.projects.map(p=>({...p,is_active:true}));
  if(path==='/adc/versions') return inventory;
  throw new Error('Unexpected '+path);
};
"""
    assertions = r"""
(async()=>{
  await loadAdcManager();
  assert.equal(adcState.activeTab,'rules');
  assert.equal(adcState.panels.rules.hidden,false);
  assert.equal(adcState.panels.projects.hidden,true);
  adcState.dirty=true; adcState.documents['.adc/index.md']='Unsaved draft';
  await adcSelectTab('versions');
  assert.equal(adcState.panels.versions.hidden,false);
  assert.equal(adcState.tabButtons.versions.attributes['aria-selected'],'true');
  assert.equal(adcState.documents['.adc/index.md'],'Unsaved draft');
  assert.equal(option.series[0].type,'sankey');
  assert.equal(option.series[0].nodeAlign,'right');
  assert.equal(option.tooltip.renderMode,'richText');
  assert.equal(option.series[0].links.length,4);
  assert.equal(option.series[0].data.filter(n=>n.name.startsWith('revision:')).length,2);
  assert.equal(option.series[0].label.color,'#ddd');
  assert.equal(option.series[0].data.find(n=>n.name==='project:8').description.includes('Unbound'),true);
  assert.throws(()=>adcVersionGraph({...inventory,revisions:[{project_id:999,release_id:1,revision:1}]}),/unresolved/);
  const ready={project_id:7,project_name:'Alpha',status:'ready',release_id:1,expected_revision:2,
    snapshot_sha256:'a'.repeat(64),changed_paths:['.adc/index.md'],document_count:1};
  api=async(method,path,body)=>{
    if(path==='/adc/rules/preview') return [structuredClone(ready)];
    assert.equal(method,'POST');
    assert.equal(body.snapshot_sha256,ready.snapshot_sha256);
    throw new Error('Local rules changed since preview');
  };
  await adcPreviewCollections();
  adcState.projectSelect.value='';
  await assert.rejects(adcCollectSnapshots,/incomplete/);
  assert.equal(adcState.collections[0].status,'blocked');
  assert.match(adcState.collections[0].error,/changed since preview/);
  assert.match(adcState.collectionStatus.textContent,/0 snapshots committed/);
  api=async()=>{throw new Error('Forbidden');};
  await assert.rejects(adcRefreshVersions,/Forbidden/);
  assert.equal(adcState.versionData,null);
  assert.equal(adcState.versionTable.children.length,0);
  disposeAdcManager();
  assert.equal(adcState,null);
  assert.equal(disposed,true);
  assert.equal(disconnected,2);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    source = "\n".join((root / file).read_text(encoding="utf-8")
                       for file in ("adc-versions.js", "adc-collection.js", "adc.js"))
    subprocess.run([node], input=setup + source + assertions,
                   check=True, capture_output=True, text=True, timeout=20)
