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
  constructor(tag='div') { this.tag=tag; this.children=[]; this.value=''; this.style={}; this.attributes={}; this.listeners={}; this.files=[]; this.clientWidth=1000; }
  append(child) { this.children.push(child); if (this.tag==='select' && !this.value && child.value) this.value=child.value; }
  replaceChildren(...children) { this.children=children; }
  insertBefore(child) { this.children.unshift(child); }
  setAttribute(name,value) { this.attributes[name]=value; }
  getAttribute(name) { return this.attributes[name]; }
  removeAttribute(name) { delete this.attributes[name]; }
  contains(child) { return this===child || this.children.some(e=>e instanceof Element && e.contains(child)); }
  getBoundingClientRect() { return {left:20,bottom:40,width:200,height:100}; }
  addEventListener(name,fn) { this.listeners[name]=fn; }
  focus() {}
  remove() {}
}
const manager = new Element();
const document = {createElement:tag=>new Element(tag), getElementById:()=>manager,
  documentElement:new Element(), body:new Element(), head:new Element(),listeners:{},
  addEventListener(name,fn){this.listeners[name]=fn;},removeEventListener(name){delete this.listeners[name];}};
document.documentElement.clientWidth=1000;
let option, disposed=false, disconnected=0;
const chart={on(){},resize(){},clear(){},dispose(){disposed=true;},setOption(value){option=value;}};
const window = {echarts:{init:()=>chart},innerHeight:900,listeners:{},addEventListener(name,fn){this.listeners[name]=fn;},removeEventListener(name){delete this.listeners[name];}};
const ResizeObserver = class {observe(){} disconnect(){disconnected++;}};
const MutationObserver = class {observe(){} disconnect(){disconnected++;}};
const getComputedStyle=()=>({getPropertyValue:name=>name==='--text'?'#ddd':'#111'});
const _me={role:'admin'}, _jwt='test-only';
const toast=()=>{}; let confirm=()=>true; const prompt=()=> 'Reviewed centralized rules';
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
  assert.equal(adcState.tabButtons.rules.textContent,'Constitution');
  assert.equal(adcState.tabButtons.projects.textContent,'Amendments');
  const workspace=adcState.panels.rules.children.find(e=>e.className==='adc-constitution-workspace');
  assert.equal(workspace.children[0].contains(adcState.documentList),true);
  assert.equal(workspace.children[0].contains(adcState.content),false);
  assert.equal(workspace.children[1].contains(adcState.content),true);
  const allElements=root=>[root,...root.children.filter(e=>e instanceof Element).flatMap(allElements)];
  const publication=allElements(manager).find(e=>e.className==='adc-publication adc-disclosure');
  assert.equal(publication.tag,'details');
  assert.equal(!!publication.open,false);
  assert.equal(publication.contains(adcState.content),false);
  assert.equal(publication.contains(adcState.releaseReason),true);
  assert.equal(adcState.documentActions.contains(adcState.content),false);
  const projectWorkspace=allElements(adcState.project).find(e=>e.className==='adc-amendment-workspace');
  assert.equal(projectWorkspace.children[0].contains(adcState.changeSelect),true);
  assert.equal(projectWorkspace.children[1].contains(adcState.changeContent),true);
  assert.equal(!!adcState.baselinePanel.open,false);
  assert.equal(!!adcState.historyPanel.open,false);
  assert.equal(!!adcState.collectionPanel.open,false);
  adcState.changePath.value='.adc/retained.md';
  adcState.changeContent.value='Retain draft while expanding tools';
  adcState.baselinePanel.open=true;
  adcState.baselinePanel.open=false;
  assert.equal(adcState.changeContent.value,'Retain draft while expanding tools');
  adcSelectChange();
  const helpRow=allElements(manager).find(e=>e.className==='adc-info-row');
  const helpButton=helpRow.children[1];
  assert.equal(helpButton.tag,'button');
  assert.match(helpButton.style.cssText,/width:24px;height:24px/);
  assert.match(helpButton.children[0].style.cssText,/width:14px;height:14px/);
  assert.equal(helpButton.children[0].attributes['aria-hidden'],'true');
  assert.equal(helpButton.attributes['aria-label'].startsWith('More information:'),true);
  assert.match(helpRow.style.cssText,/align-items:center/);
  helpButton.listeners.focus();
  assert.equal(adcInfoTooltip.root.attributes.role,'tooltip');
  assert.equal(adcInfoTooltip.root.children[1].tag,'ul');
  assert.equal(adcInfoTooltip.root.children[1].children[0].children[0].tag,'strong');
  assert.equal(helpButton.attributes['aria-describedby'],'adc-info-tooltip');
  document.listeners.keydown({key:'Escape'});
  assert.equal(adcInfoTooltip.root.hidden,true);
  assert.equal(helpButton.attributes['aria-describedby'],undefined);
  helpButton.listeners.click();
  assert.equal(adcInfoTooltip.pinned,true);
  helpButton.listeners.click();
  assert.equal(adcInfoTooltip.root.hidden,true);
  helpButton.listeners.click();
  document.listeners.pointerdown({target:new Element()});
  assert.equal(adcInfoTooltip.root.hidden,true);
  const injection=adcInfoText(manager,'Literal input',[
    ['Source','<img src=x onerror=alert(1)>\nSecond line'],['Hash','a1b2','code']]);
  const injectionButton=manager.children.at(-1).children[1];
  injectionButton.listeners.focus();
  assert.equal(adcInfoTooltip.root.children[1].children[0].children[1].textContent,'<img src=x onerror=alert(1)>\nSecond line');
  assert.equal(adcInfoTooltip.root.children[1].children[0].children[1].style.whiteSpace,'pre-wrap');
  assert.equal(adcInfoTooltip.root.children[1].children[1].children[1].tag,'code');
  assert.equal(injection.tag,'span');
  injectionButton.listeners.pointerleave();
  adcInfoTooltip.root.listeners.pointerenter();
  await new Promise(resolve=>setTimeout(resolve,200));
  assert.equal(adcInfoTooltip.root.hidden,false);
  window.listeners.scroll({target:adcInfoTooltip.root});
  assert.equal(adcInfoTooltip.root.hidden,false);
  adcInfoTooltip.root.listeners.pointerleave();
  assert.equal(adcInfoTooltip.root.hidden,true);
  adcHideInfo();
  adcState.dirty=true; adcState.documents['.adc/index.md']='Unsaved draft';
  await adcSelectTab('versions');
  assert.equal(adcState.panels.versions.hidden,false);
  assert.equal(adcState.tabButtons.versions.attributes['aria-selected'],'true');
  assert.equal(adcState.documents['.adc/index.md'],'Unsaved draft');
  assert.equal(option.series[0].type,'sankey');
  assert.equal(option.series[0].nodeAlign,'right');
  assert.equal(option.tooltip.renderMode,'richText');
  assert.equal(option.series[0].links.length,2);
  assert.equal(option.series[0].data.filter(n=>n.name.startsWith('revision:')).length,1);
  assert.equal(option.series[0].data.find(n=>n.name.startsWith('revision:')).revision,2);
  assert.match(adcState.versionSummary.textContent,/Current revisions only/);
  adcState.versionScope.value='history';
  adcApplyVersionFilters(); await adcRenderVersionChart();
  assert.equal(option.series[0].links.length,4);
  adcState.versionSearch.value='alpha';
  adcApplyVersionFilters(); await adcRenderVersionChart();
  assert.equal(option.series[0].data.some(n=>n.name==='project:8'),false);
  assert.match(adcState.versionSummary.textContent,/1 of 2 projects/);
  adcState.versionSearch.value='8';
  adcApplyVersionFilters();
  assert.equal(adcState.versionView.projects[0].id,8);
  assert.equal(adcState.versionView.revisions.length,0);
  adcState.versionSearch.value='no-match';
  adcApplyVersionFilters();
  assert.equal(adcState.versionView.projects.length,0);
  assert.equal(adcState.versionTable.children[1].children[0].children[0].textContent,'No projects match this filter.');
  adcState.versionSearch.value='';
  adcApplyVersionFilters(); await adcRenderVersionChart();
  assert.equal(option.series[0].label.color,'#ddd');
  assert.equal(option.series[0].data.find(n=>n.name==='project:8').description.includes('Unbound'),true);
  adcState.chartRoot.clientWidth=300;
  await adcRenderVersionChart();
  assert.equal(option.series[0].right,110);
  assert.equal(option.series[0].label.overflow,'truncate');
  assert.match(adcState.chartRoot.style.cssText,/overflow:hidden/);
  adcState.chartRoot.clientWidth=1000;
  adcState.dirty=false;
  adcState.documents={'.adc/one.md':'One','.adc/two.md':'Two\r\nSecond line'};
  adcRenderDocuments('.adc/one.md');
  const documentButton=path=>adcState.documentList.children.find(e=>e.attributes['data-adc-document']===path);
  assert.equal(documentButton('.adc/one.md').attributes['aria-current'],'page');
  adcState.content.value='Unstaged';
  confirm=()=>false;
  await documentButton('.adc/two.md').listeners.click();
  assert.equal(adcState.editorPath,'.adc/one.md');
  assert.equal(documentButton('.adc/one.md').attributes['aria-current'],'page');
  assert.equal(adcState.content.value,'Unstaged');
  assert.equal(adcHasRuleDraft(),true);
  adcState.documentSearch.value='TWO';
  adcState.documentSearch.listeners.input();
  assert.equal(adcState.documentList.children.length,1);
  assert.equal(documentButton('.adc/two.md').attributes['aria-current'],undefined);
  assert.equal(adcState.content.value,'Unstaged');
  adcState.documentSearch.value='no-match';
  adcState.documentSearch.listeners.input();
  assert.equal(adcState.documentList.children[0].textContent,'No documents match this filter.');
  assert.equal(adcState.editorPath,'.adc/one.md');
  adcState.documentSearch.value='';
  adcState.documentSearch.listeners.input();
  let prevented=false;
  window.listeners.beforeunload({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);
  confirm=()=>true;
  await documentButton('.adc/two.md').listeners.click();
  adcState.content.value=adcState.content.value.replace(/\r\n?/g,'\n');
  assert.equal(adcState.content.value,'Two\nSecond line');
  assert.equal(adcRuleEditorDirty(),false);
  assert.equal(adcState.documents['.adc/two.md'],'Two\r\nSecond line');
  assert.equal(documentButton('.adc/two.md').attributes['aria-current'],'page');
  assert.equal(documentButton('.adc/one.md').attributes['aria-current'],undefined);
  adcState.changePath.value='.adc/project.md'; adcState.changeReason.value='Unstaged justification';
  adcState.changeSelect.value='other';
  confirm=()=>false; adcState.changeSelect.listeners.change();
  assert.equal(adcState.changeSelect.value,'');
  assert.equal(adcState.changePath.value,'.adc/project.md');
  assert.equal(adcHasProjectDraft(),true);
  adcState.projectSelect.value='7';
  await adcState.projectSelect.listeners.change();
  assert.equal(adcState.changePath.value,'.adc/project.md');
  adcState.projectSelect.value='';
  const projectButtons=allElements(adcState.project).filter(e=>e.tag==='button');
  await projectButtons.find(e=>e.textContent==='Save new project revision').listeners.click();
  assert.match(adcState.error.textContent,/Choose a project/);
  adcState.current={project_id:7,revision:2,release:{id:1}};
  await projectButtons.find(e=>e.textContent==='Save new project revision').listeners.click();
  assert.match(adcState.error.textContent,/Stage the edited project change/);
  confirm=()=>true;
  adcSelectChange();
  adcState.targetRelease.value='2';
  adcState.baselinePanel.open=false;
  await projectButtons.find(e=>e.textContent==='Save new project revision').listeners.click();
  assert.match(adcState.error.textContent,/Preview and acknowledge baseline differences/);
  assert.equal(!!adcState.baselinePanel.open,false);
  adcState.targetRelease.value='1';
  prevented=false;
  window.listeners.beforeunload({preventDefault(){prevented=true;}});
  assert.equal(prevented,false);
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
  assert.equal(window.listeners.beforeunload,undefined);
  assert.equal(window.listeners.resize,undefined);
  assert.equal(adcInfoTooltip,null);
  assert.equal(document.listeners.pointerdown,undefined);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    source = "\n".join((root / file).read_text(encoding="utf-8")
                       for file in ("adc-versions.js", "adc-collection.js", "adc.js"))
    subprocess.run([node], input=setup + source + assertions,
                   check=True, capture_output=True, text=True, timeout=20)
