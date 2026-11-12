from pathlib import Path
import re
import shutil
import subprocess

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for executable plugin UI tests")
    result = subprocess.run([node], input=script, capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def report_workspace_script(test: str) -> str:
    source = (FRONTEND / "report-workspace.js").read_text(encoding="utf-8")
    source = source.replace("import('/report-vendor/report-libraries.js')", "Promise.resolve(reportLibraries)")
    return r"""
const assert = require('node:assert/strict');
let _jwt='session',_me={role:'admin'};
const _projectNameById={P1:'Example'},_allProjects=[];
const failures=[],downloads=[],elements=new Map(),calls=[];
const exportButtons=[{disabled:true},{disabled:true}];
let sections=[{value:'activity'},{value:'readiness'},{value:'evidence'}];
function element(){
  return {children:[],textContent:'',value:'',hidden:false,dataset:{},attributes:{},
    get selectedOptions(){return this.children.filter(option=>option.selected);},
    get options(){return this.children;},append(child){this.children.push(child);},
    replaceChildren(){this.children=[];},setAttribute(key,value){this.attributes[key]=value;},
    getAttribute(key){return this.attributes[key];},remove(){},click(){
      if(this.download)downloads.push({name:this.download,blob:blobs.get(this.href)});
    }};
}
const document={documentElement:element(),body:element(),
  getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},
  createElement:element,querySelectorAll(selector){
    return selector==='[data-report-export]'?exportButtons:selector==='[data-report-section]:checked'?sections:[];
  }};
const el=id=>document.getElementById(id);
el('app-version').textContent='v1.30.132';
el('report-start').value='2026-11-01';el('report-end').value='2026-11-07';
el('report-period').value='7';el('report-results').hidden=true;
const _fmtIso=value=>value;
const toast=message=>failures.push(message);
class ResizeObserver{observe(){}}
class MutationObserver{observe(){}}
const matchMedia=()=>({addEventListener(){}});
const getComputedStyle=()=>({getPropertyValue:()=> '#17263a'});
const blobs=new Map();
URL.createObjectURL=blob=>{const key='blob:'+blobs.size;blobs.set(key,blob);return key;};
URL.revokeObjectURL=()=>{};
let activityWait=null;
const activity={activities:[{occurred_at:'2026-11-02T00:00:00Z',project_id:'P1',
  event_type:'review',status:'done',title:'\n =SUM(1,2)',summary:'"quoted"',activity_id:'A1'}],
  counts:{events:1,projects:1,by_type:{review:1},by_status:{done:1},by_project:{P1:1},by_day:{'2026-11-02':1}},
  coverage:{truncated:false,note:'Recorded evidence',scope:'Not ROI'}};
const projects=[{project:{project_name:'Example'},overall_score:null,overall_status:'unknown',
  dimensions:[{label:'Context',score:null,status:'unknown',signals:[]}],recommended_next_actions:[]}];
const api=async(method,path,body)=>{
  if(method==='POST'){calls.push(body);return activityWait||activity;}
  return {projects};
};
const reportLibraries={echarts:{init(){return {setOption(){},on(){},dispose(){},resize(){}};}}};
""" + source + "\n" + test


def test_report_workspace_checks_dates_and_does_not_export_changed_filters():
    run_node(report_workspace_script(r"""
(async()=>{
  ReportWorkspace.range();
  assert.equal((new Date(el('report-end').value)-new Date(el('report-start').value))/86400000,6);
  el('report-start').value='2026-11-08';el('report-end').value='2026-11-07';
  await ReportWorkspace.generate();
  assert.equal(calls.length,0);
  assert.match(el('report-status').textContent,/valid UTC date/);
  el('report-start').value='2026-11-01';
  const saved=sections;sections=[];
  await ReportWorkspace.generate();
  assert.equal(calls.length,0);
  assert.match(el('report-status').textContent,/at least one/);
  sections=saved;
  await ReportWorkspace.generate();
  assert.equal(calls[0].end,'2026-11-08T00:00:00.000Z');
  assert.equal(el('report-kpis').children[2].children[1].textContent,'Unknown');
  await ReportWorkspace.exportReport('csv');
  assert.equal(downloads[0].name,'CGA-Report-v1.30.132-en.csv');
  const csv=await downloads[0].blob.text();
  assert.ok(csv.includes("\"'\n =SUM(1,2)\""));
  assert.ok(csv.includes('""' + '"quoted"' + '""'));
  el('report-search').value='changed';
  ReportWorkspace.buttons();
  assert.ok(exportButtons.every(button=>button.disabled));
  await ReportWorkspace.exportReport('json');
  assert.equal(downloads.length,1);
  el('report-search').value='';ReportWorkspace.buttons();
  assert.ok(exportButtons.every(button=>!button.disabled));
  assert.doesNotMatch(el('report-status').textContent,/Filters changed/);
  activity.counts.by_status={'done"] --> evil["oops':1};
  await ReportWorkspace.generate();
  await ReportWorkspace.exportReport('mmd');
  const diagram=await downloads[1].blob.text();
  assert.ok(diagram.includes('#34;#93;'));
  assert.ok(!diagram.includes('evil["oops'));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""))


def test_report_workspace_keeps_inflight_filter_changes_visibly_unapplied():
    run_node(report_workspace_script(r"""
(async()=>{
  await ReportWorkspace.generate();
  let release;
  activityWait=new Promise(resolve=>{release=resolve;});
  const pending=ReportWorkspace.generate();
  ReportWorkspace.buttons();
  assert.ok(exportButtons.every(button=>button.disabled));
  await ReportWorkspace.exportReport('json');
  assert.equal(downloads.length,0);
  el('report-search').value='edited during generation';
  release(activity);
  await pending;
  assert.match(el('report-status').textContent,/Filters changed/);
  assert.ok(exportButtons.every(button=>button.disabled));
  assert.equal(calls[0].search,'');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""))


def test_report_workspace_rejects_stale_generation_and_pdf_after_session_reset():
    run_node(report_workspace_script(r"""
(async()=>{
  let release;
  activityWait=new Promise(resolve=>{release=resolve;});
  const pending=ReportWorkspace.generate();
  ReportWorkspace.reset();_jwt='new-session';
  release(activity);await pending;
  assert.equal(el('report-results').hidden,true);
  activityWait=null;
  await ReportWorkspace.generate();
  let finishLibraries;
  reportLibraries.exportLibraries=()=>new Promise(resolve=>{finishLibraries=resolve;});
  const exporting=ReportWorkspace.exportReport('pdf');
  await Promise.resolve();await Promise.resolve();
  assert.equal(typeof finishLibraries,'function');
  ReportWorkspace.reset();_jwt='';_me=null;
  finishLibraries({mermaid:{},html2canvas(){},jsPDF(){}});
  await exporting;
  assert.equal(downloads.length,0);
  assert.equal(el('report-results').hidden,true);
  assert.match(failures[0],/cancelled because the session or report snapshot changed/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""))


def test_report_plugin_preserves_generation_template_and_existing_history():
    source = (FRONTEND / "plugins" / "reports.js").read_text(encoding="utf-8")
    run_node(r"""
const assert = require('node:assert/strict');
const _projectNameById = {P1:'Example'};
const history = JSON.stringify([{md:'Existing report',stamp:'2026-11-01'}]);
const storage = new Map([['cga_wsr_history',history]]);
const localStorage = {getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)};
const document = {getElementById:()=>null};
""" + source + r"""
const markdown = buildWsrMarkdown({activities:[
  {project_id:'P1',occurred_at:'2026-11-08T12:00:00Z',event_type:'review',status:'done',title:'Reviewed change'},
  {project_id:'P1',occurred_at:'2026-10-01T12:00:00Z',event_type:'review',status:'done',title:'Out of window'},
]},{windowStart:'2026-11-05T00:00:00Z',windowEnd:'2026-11-12T00:00:00Z'});
assert.match(markdown,/Reviewed change/);
assert.match(markdown,/Events in window: \*\*1\*\*/);
assert.doesNotMatch(markdown,/Out of window/);
assert.equal(storage.get('cga_wsr_history'),history);
assert.equal(readWorkReportHistory()[0].md,'Existing report');
storage.set('cga_wsr_template','# {{project_names}}\n{{highlights}}\n{{custom}}');
const custom=buildWsrMarkdown({activities:[]},{days:7});
assert.match(custom,/\{\{custom\}\}/);
assert.doesNotMatch(custom,/## Summary/);
""")


def test_plugin_assets_are_lazy_deduplicated_and_retry_after_failed_load():
    source = (FRONTEND / "plugins.js").read_text(encoding="utf-8")
    run_node(r"""
const assert = require('node:assert/strict');
const appended=[];
const document={createElement:()=>({remove(){this.removed=true;}}),head:{append:script=>appended.push(script)}};
""" + source + r"""
(async()=>{
  await assert.rejects(loadPluginAsset('reports'),/not installed/);
  assert.equal(appended.length,0);
  _pluginSelection={installed:['reports'],enabled:['reports']};
  const first=loadPluginAsset('reports'),second=loadPluginAsset('reports');
  assert.equal(first,second);
  assert.equal(appended.length,1);
  assert.equal(appended[0].src,'/plugins/reports.js');
  appended[0].onerror();
  await assert.rejects(first,/Could not load/);
  const retry=loadPluginAsset('reports');
  assert.equal(appended.length,2);
  appended[1].onload();
  await retry;
  _pluginSelection={installed:[],enabled:[]};
  await assert.rejects(loadPluginAsset('reports'),/disabled/);
  assert.equal(pluginAllowsTab('audit'),true);
  assert.equal(pluginAllowsTab('adc'),true);
  assert.equal(pluginAllowsTab('report'),false);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_schedule_editor_chooses_enabled_adapter_and_reports_missing_adapters():
    markup = (FRONTEND / "index.html").read_text(encoding="utf-8")
    match = re.search(r"function openScheduleEditor\([^)]*\) \{.*?^\}", markup, re.M | re.S)
    assert match is not None
    run_node(r"""
const assert = require('node:assert/strict');
let _scheduleEditingId=null;
const _scheduleTasks=[],errors=[];
const elements=new Map();
const document={getElementById(id){
  if(!elements.has(id))elements.set(id,{value:'',hidden:true,scrollIntoView(){},focus(){this.focused=true;}});
  return elements.get(id);
}};
const select=document.getElementById('schedule-task-type');
select.options=[{value:'browseragent_task',disabled:true},{value:'http_request',disabled:false}];
function renderScheduleProjectOptions(){}
function defaultSchedulePayload(type){assert.equal(type,'http_request');return {action:'inspect'};}
function applyScheduleTypeDefaults(){}
let view='history';
function selectScheduleTab(name){view=name;}
function toast(message){errors.push(message);}
""" + match.group() + r"""
openScheduleEditor();
assert.equal(select.value,'http_request');
assert.equal(document.getElementById('schedule-editor').hidden,false);
assert.equal(view,'tasks');
assert.equal(document.getElementById('schedule-name').focused,true);
assert.deepEqual(JSON.parse(document.getElementById('schedule-payload').value),{action:'inspect'});
document.getElementById('schedule-editor').hidden=true;
select.options.forEach(option=>option.disabled=true);
openScheduleEditor();
assert.equal(document.getElementById('schedule-editor').hidden,true);
assert.match(errors[0],/Install and enable a task adapter/);
""")


def test_automation_keeps_policy_inputs_in_audit_and_prioritizes_tasks():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    automation = html.split('id="pane-schedule"', 1)[1].split('<!-- USERS -->', 1)[0]
    readiness = html.split('id="pane-ai-first"', 1)[1].split('id="pane-report"', 1)[0]
    for field in ("ai-first-policy-profile", "ai-first-policy-notes", "ai-first-signals-body",
                  "ai-first-signal-type", "ai-first-github-repo-url", "ai-first-ado-repo-url"):
        assert f'id="{field}"' not in automation
        assert f'id="{field}"' in readiness
        assert html.count(f'id="{field}"') == 1
    assert 'id="schedule-history-panel" role="tabpanel" aria-labelledby="schedule-tab-history" hidden' in automation
    assert automation.index('id="schedule-editor"') < automation.index('id="schedule-body"')
    assert '<details class="schedule-advanced' in automation


def test_automation_tabs_preserve_editor_and_filter_state_with_keyboard_navigation():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    functions = []
    for name in ("selectScheduleTab", "handleScheduleTabKey"):
        match = re.search(rf"function {name}\([^)]*\) \{{.*?^\}}", html, re.M | re.S)
        assert match is not None
        functions.append(match.group())
    run_node(r"""
const assert=require('node:assert/strict');
const panels=new Map(['tasks','history'].map(name=>['schedule-'+name+'-panel',{hidden:name!=='tasks'}]));
const buttons=['tasks','history'].map(name=>({
 dataset:{scheduleTab:name},attrs:{'aria-controls':'schedule-'+name+'-panel'},tabIndex:-1,
 setAttribute(key,value){this.attrs[key]=value;},getAttribute(key){return this.attrs[key];},
 focus(){this.focused=true;},closest(){return this;},
}));
const editor={hidden:false,value:'Unsaved task'};
const filter={value:'smoke'};
panels.set('schedule-editor',editor);panels.set('schedule-filter-search',filter);
const document={querySelectorAll:()=>buttons,getElementById:id=>panels.get(id)};
""" + "\n".join(functions) + r"""
selectScheduleTab('history');
assert.equal(panels.get('schedule-tasks-panel').hidden,true);
assert.equal(panels.get('schedule-history-panel').hidden,false);
assert.equal(buttons[1].attrs['aria-selected'],'true');
assert.equal(buttons[1].tabIndex,0);
assert.equal(buttons[0].tabIndex,-1);
let prevented=false;
handleScheduleTabKey({target:buttons[1],key:'ArrowLeft',preventDefault(){prevented=true;}});
assert.equal(prevented,true);
assert.equal(panels.get('schedule-tasks-panel').hidden,false);
assert.equal(buttons[0].focused,true);
handleScheduleTabKey({target:buttons[0],key:'End',preventDefault(){}});
assert.equal(buttons[1].attrs['aria-selected'],'true');
handleScheduleTabKey({target:buttons[1],key:'Home',preventDefault(){}});
assert.equal(buttons[0].attrs['aria-selected'],'true');
assert.deepEqual(editor,{hidden:false,value:'Unsaved task'});
assert.equal(filter.value,'smoke');
assert.throws(()=>selectScheduleTab('unknown'),/Unknown Automation view/);
""")


def test_automation_compact_rows_retain_details_actions_search_and_expanded_state():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    match = re.search(r"function renderSchedules\([^)]*\) \{.*?^\}", html, re.M | re.S)
    assert match is not None
    run_node(r"""
const assert=require('node:assert/strict');
const elements=new Map();
const document={getElementById(id){
 if(!elements.has(id))elements.set(id,{value:'',dataset:{},innerHTML:'',querySelectorAll:()=>[]});
 return elements.get(id);
}};
const _scheduleTasks=[{id:7,task_id:'task-7',name:'Nightly smoke',project_name:'Demo',
 task_type:'browseragent_task',cadence_minutes:60,enabled:false,last_run_status:'failed',
 last_run_at:'last-time',next_run_at:'next-time',target_url:'http://runner.invalid',
 payload:{browser_task:{url:'https://page.invalid',goal:'Check page'}}}];
const _scheduleRuns=[{schedule_id:7,task_id:'task-7',status:'failed',error:'Timed out'}];
function esc(value){return String(value??'');}
function browserTaskSpecFromPayload(payload){return payload.browser_task;}
function scheduleSummaryStatusTone(status){return status;}
function scheduleStatusClass(){return 'pill-red';}
function scheduleTypeLabel(){return 'BrowserAgent';}
function renderScheduleTaskIdWithCopy(id){return 'COPY-'+id;}
function scheduleActionButton(action,label){return '<button aria-label="'+label+'">'+action+'</button>';}
function _fmtIso(value){return value||'—';}
""" + match.group() + r"""
const body=document.getElementById('schedule-body');
body.querySelectorAll=()=>[{dataset:{scheduleDetails:'7'}}];
renderSchedules();
assert.equal((body.innerHTML.match(/<td\b/g)||[]).length,5);
for(const text of ['Demo','BrowserAgent','COPY-task-7','http://runner.invalid','https://page.invalid',
 'Every 60 min','Disabled','last-time','next-time','Run schedule now','Retry failed schedule now',
 'Enable schedule','Edit schedule','Delete schedule'])assert.ok(body.innerHTML.includes(text),text);
assert.match(body.innerHTML,/data-schedule-details="7" open/);
assert.match(document.getElementById('schedule-runs-body').innerHTML,/Timed out/);
document.getElementById('schedule-filter-search').value='page.invalid';
renderSchedules();
assert.match(body.innerHTML,/Nightly smoke/);
document.getElementById('schedule-filter-search').value='absent';
renderSchedules();
assert.match(body.innerHTML,/colspan="5".*No matching schedules/);
""")


def test_extension_sections_do_not_mix_monitor_configuration_with_plugin_management():
    source = (FRONTEND / "plugins.js").read_text(encoding="utf-8")
    run_node(r"""
const assert=require('node:assert/strict');
const elements=new Map();
const document={getElementById(id){
 if(!elements.has(id))elements.set(id,{hidden:false,setAttribute(key,value){this[key]=value;}});
 return elements.get(id);
}};
""" + source + r"""
_pluginSelection={installed:['azure_policy_change_monitor'],enabled:['azure_policy_change_monitor']};
switchExtensionView('monitor');
assert.equal(elements.get('extension-monitor-workspace').hidden,false);
assert.equal(elements.get('extension-plugin-workspace').hidden,true);
assert.equal(elements.get('extension-monitor-btn')['aria-pressed'],'true');
switchExtensionView('plugins');
assert.equal(elements.get('extension-monitor-workspace').hidden,true);
assert.equal(elements.get('extension-plugin-workspace').hidden,false);
_pluginSelection={installed:[],enabled:[]};
switchExtensionView('monitor');
assert.equal(elements.get('extension-monitor-workspace').hidden,true);
assert.equal(elements.get('extension-plugin-workspace').hidden,false);
""")


def test_plugin_changes_update_status_and_save_only_current_selection():
    source = (FRONTEND / "plugins.js").read_text(encoding="utf-8")
    run_node(r"""
const assert=require('node:assert/strict');
const elements=new Map(),badge={};
let selected={installed:['reports'],enabled:['reports']},saved,reloaded=false;
const root={querySelector:()=>badge,querySelectorAll(selector){
 const field=selector.includes('"installed"')?'installed':'enabled';
 return selected[field].map(id=>({dataset:{plugin:id}}));
}};
const document={getElementById(id){
 if(id==='plugin-catalog')return root;
 if(!elements.has(id))elements.set(id,{});
 return elements.get(id);
}};
const window={location:{reload(){reloaded=true;}}};
async function api(method,path,body){saved={method,path,body};}
function toast(){throw new Error('Unexpected save error');}
""" + source + r"""
(async()=>{
_pluginCatalog=[{id:'reports',available:true}];
_pluginSelection={installed:['reports'],enabled:['reports']};
updatePluginSelectionStatus();
assert.equal(badge.textContent,'Enabled');
assert.equal(elements.get('plugin-save-btn').disabled,true);
selected={installed:['reports'],enabled:[]};
updatePluginSelectionStatus();
assert.equal(badge.textContent,'Disabled');
assert.equal(elements.get('plugin-save-btn').disabled,false);
assert.match(elements.get('plugin-selection-status').textContent,/Unsaved/);
selected={installed:[],enabled:[]};
updatePluginSelectionStatus();
assert.equal(badge.textContent,'Not installed');
await savePluginSelection(elements.get('plugin-save-btn'));
assert.deepEqual(saved,{method:'PUT',path:'/admin/plugins',body:{installed:[],enabled:[]}});
assert.equal(reloaded,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_task_defaults_apply_to_new_tasks_without_rewriting_existing_endpoints():
    markup = (FRONTEND / "index.html").read_text(encoding="utf-8")
    functions = "\n".join(re.search(
        rf"function {name}\([^)]*\) \{{.*?^\}}", markup, re.M | re.S,
    ).group() for name in ("defaultSchedulePayload", "defaultScheduleTarget", "applyScheduleTypeDefaults"))
    run_node(r"""
const assert=require('node:assert/strict');
let _scheduleEditingId=null;
const _pluginRuntimeSettings={workflows:{agent_url:'https://new.invalid/activate',http_url:'https://new.invalid/run',timeout_seconds:81,payload:{action:'inspect'}}};
const elements={
 'schedule-task-type':{value:'agent_activation'},'schedule-target-url':{value:''},
 'schedule-payload':{value:''},'schedule-timeout':{value:30},
};
const document={getElementById:id=>elements[id]};
function toggleBrowserTaskEditor(){}
""" + functions + r"""
applyScheduleTypeDefaults(true);
assert.equal(elements['schedule-target-url'].value,'https://new.invalid/activate');
assert.equal(elements['schedule-timeout'].value,81);
assert.equal(JSON.parse(elements['schedule-payload'].value).action,'inspect');
_scheduleEditingId='existing-task';
elements['schedule-target-url'].value='http://localhost:3002/activate';
elements['schedule-payload'].value='{"action":"existing"}';
elements['schedule-timeout'].value=43;
applyScheduleTypeDefaults(false);
assert.equal(elements['schedule-target-url'].value,'http://localhost:3002/activate');
assert.equal(elements['schedule-timeout'].value,43);
assert.equal(elements['schedule-payload'].value,'{"action":"existing"}');
""")


def test_plugin_setting_save_reports_errors_and_preserves_report_history():
    source = (FRONTEND / "plugins.js").read_text(encoding="utf-8")
    run_node(r"""
const assert=require('node:assert/strict');
const storage=new Map([['cga_wsr_history','[{"md":"Retained"}]']]),elements=new Map(),errors=[];
const localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)};
const document={getElementById(id){if(!elements.has(id))elements.set(id,{value:''});return elements.get(id);}};
function toast(message){errors.push(message);}
let fail=true,request;
async function api(method,path,body){if(fail)throw new Error('Rejected');request={method,path,body};}
""" + source + r"""
(async()=>{
 const values=[{name:'timeout_seconds',dataset:{settingType:'number'},value:'81'}];
 const form={querySelectorAll:selector=>selector.includes('="json"')?[]:values};
 const button={disabled:false};
 _pluginDetailId='browseragent';
 await savePluginSettings(form,button);
 assert.equal(button.disabled,false);
 assert.match(elements.get('plugin-settings-status').textContent,/Not saved: Rejected/);
 assert.equal(_pluginDetailSaved,'');
 fail=false;
 await savePluginSettings(form,button);
 assert.equal(request.body.timeout_seconds,81);
 assert.equal(_pluginRuntimeSettings.browseragent.timeout_seconds,81);
 assert.equal(elements.get('plugin-settings-status').textContent,'Settings saved');
 _pluginDetailId='reports';
 values.splice(0,values.length,
  {name:'template',dataset:{settingType:'textarea'},value:'# Custom'},
  {name:'auto_interval',dataset:{settingType:'select'},value:'0'},
  {name:'loop_url',dataset:{settingType:'url'},value:'https://loop.example.invalid/'});
 await savePluginSettings(form,button);
 assert.equal(storage.get('cga_wsr_template'),'# Custom');
 assert.equal(storage.get('cga_wsr_history'),'[{"md":"Retained"}]');
 assert.equal(JSON.parse(storage.get('cga_reports_preferences')).auto_interval,'0');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_briefing_runtime_preferences_control_initial_filters_and_polling():
    source = (FRONTEND / "plugins.js").read_text(encoding="utf-8")
    run_node(r"""
const assert=require('node:assert/strict');
const elements=new Map(),_jwt='fixture',_me={role:'admin'};
const document={getElementById(id){if(!elements.has(id))elements.set(id,{value:''});return elements.get(id);}};
let _briefingPollInterval=12,stopped=[],started=[],loads=[];
function clearInterval(id){stopped.push(id);}
function setInterval(callback,ms){started.push(ms);return 13;}
function toast(message){throw new Error(message);}
let defaults={project_id:'P1',refresh_seconds:17,batch_size:9};
async function api(){return {values:defaults};}
async function loadBriefingPlugin(options){loads.push(options);}
""" + source + r"""
(async()=>{
 _pluginSelection={installed:['briefing'],enabled:['briefing']};
 _pluginAssets.set('briefing',Promise.resolve());
 await loadWorkBriefing({useDefaults:true});
 assert.equal(elements.get('briefing-project-id').value,'P1');
 assert.equal(elements.get('briefing-limit').value,9);
 assert.deepEqual(started,[17000]);
 defaults={project_id:'',refresh_seconds:0,batch_size:3};
 await loadWorkBriefing({useDefaults:true});
 assert.equal(_briefingPollInterval,null);
 assert.deepEqual(stopped,[12,13]);
 assert.equal(loads.length,2);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_plugin_detail_routes_preserve_or_discard_unsaved_settings_explicitly():
    source = (FRONTEND / "plugins.js").read_text(encoding="utf-8")
    run_node(r"""
const assert=require('node:assert/strict');
const input={name:'timeout_seconds',dataset:{settingType:'number'},value:'81'};
const form={querySelectorAll:()=>[input]};
const elements=new Map([['plugin-settings-form',form]]);
const document={getElementById(id){
 if(!elements.has(id))elements.set(id,{hidden:false,classList:{toggle(){}},querySelectorAll:()=>[],
 replaceChildren(){elements.delete('plugin-settings-form');}});
 return elements.get(id);
}};
let discard=false;
const window={location:{href:'http://fixture/admin/extensions?plugin=browseragent'},
 confirm:()=>discard,history:{pushState(state,title,url){window.location.href=new URL(url,window.location.href).href;}}};
""" + source + r"""
(async()=>{
 _pluginDetailId='browseragent';
 _pluginDetailSaved='{"timeout_seconds":30}';
 assert.equal(closePluginDetails(),false);
 assert.equal(_pluginDetailId,'browseragent');
 assert.match(window.location.href,/plugin=browseragent/);
 discard=true;
 assert.equal(closePluginDetails(),true);
 assert.equal(_pluginDetailId,'');
 assert.equal(new URL(window.location.href).search,'');
 assert.equal(document.getElementById('plugin-detail-workspace').hidden,true);
 window.location.href='http://fixture/admin/extensions?plugin=reports';
 let opened;
 openPluginDetails=async(id,update)=>{opened={id,update};};
 await syncPluginDetailFromLocation();
 assert.deepEqual(opened,{id:'reports',update:false});
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_catalog_combines_search_category_status_and_natural_sort_for_hundreds_of_plugins():
    source = (FRONTEND / "plugins.js").read_text(encoding="utf-8")
    run_node(r"""
const assert=require('node:assert/strict');
""" + source + r"""
_pluginCatalog=Array.from({length:500},(_,index)=>({
 id:`plugin_${index}`,name:`Plugin ${index}`,description:'HTTP workflow checks',
 category:index%2?'Integrations':'Monitoring',available:index!==499,
}));
const selection={installed:['plugin_2','plugin_10','plugin_11'],enabled:['plugin_10']};
const defaults={search:'',category:'',state:'',sort:'name'};
const list=filters=>pluginCatalogView(selection,{...defaults,...filters});
assert.equal(list({}).length,500);
assert.deepEqual(list({search:'  PLUGIN_10  HTTP ',category:'Monitoring',state:'enabled'}).map(p=>p.id),['plugin_10']);
assert.equal(list({search:'plugin_10',category:'Integrations',state:'enabled'}).length,0);
assert.deepEqual(list({state:'disabled'}).map(p=>p.id),['plugin_2','plugin_11']);
assert.deepEqual(list({state:'installed',sort:'name-desc'}).map(p=>p.id),['plugin_11','plugin_10','plugin_2']);
assert.deepEqual(list({state:'not-bundled'}).map(p=>p.id),['plugin_499']);
assert.equal(list({state:'not-installed'}).length,496);
assert.deepEqual(list({sort:'status'}).slice(0,3).map(p=>p.id),['plugin_10','plugin_2','plugin_11']);
assert.equal(list({sort:'status'}).at(-1).id,'plugin_499');
assert.equal(list({sort:'category'})[0].category,'Integrations');
assert.equal(list({search:'no matching capability'}).length,0);
assert.deepEqual(selection,{installed:['plugin_2','plugin_10','plugin_11'],enabled:['plugin_10']});
""")


def test_filtering_sorting_and_reset_keep_hidden_unsaved_selections_in_save_payload():
    source = (FRONTEND / "plugins.js").read_text(encoding="utf-8")
    run_node(r"""
const assert=require('node:assert/strict');
const elements=new Map(),badges=new Map();
let cards=['reports','workflows','browseragent'].map(id=>({dataset:{pluginCard:id},hidden:false}));
const inputs=[
 {dataset:{plugin:'reports',field:'installed'},checked:true},
 {dataset:{plugin:'reports',field:'enabled'},checked:false},
 {dataset:{plugin:'workflows',field:'installed'},checked:true},
 {dataset:{plugin:'workflows',field:'enabled'},checked:true},
 {dataset:{plugin:'browseragent',field:'installed'},checked:false},
 {dataset:{plugin:'browseragent',field:'enabled'},checked:false},
];
const root={
 querySelector(selector){const id=selector.match(/="([^"]+)"/)[1];if(!badges.has(id))badges.set(id,{});return badges.get(id);},
 querySelectorAll(selector){
  if(selector==='[data-plugin-card]')return cards;
  const field=selector.includes('"installed"')?'installed':'enabled';
  return inputs.filter(input=>input.dataset.field===field&&input.checked);
 },
 append(...nodes){cards=[...cards.filter(card=>!nodes.includes(card)),...nodes];},
 contains:()=>false,
};
const document={getElementById(id){
 if(id==='plugin-catalog')return root;
 if(!elements.has(id))elements.set(id,{value:'',hidden:false});
 return elements.get(id);
}};
let saved,reloaded=false;
async function api(method,path,body){saved={method,path,body};}
const window={location:{reload(){reloaded=true;}}};
function toast(message){throw new Error(message);}
""" + source + r"""
(async()=>{
_pluginCatalog=[
 {id:'reports',name:'Reports',description:'Weekly updates',available:true},
 {id:'workflows',name:'Workflows',description:'HTTP tasks',available:true},
 {id:'browseragent',name:'BrowserAgent',description:'Browser tasks',available:true},
];
_pluginSelection={installed:['reports'],enabled:['reports']};
document.getElementById('plugin-sort').value='name';
document.getElementById('plugin-state-filter').value='enabled';
updatePluginSelectionStatus();
assert.deepEqual(cards.filter(card=>!card.hidden).map(card=>card.dataset.pluginCard),['workflows']);
assert.equal(badges.get('reports').textContent,'Disabled');
assert.match(elements.get('plugin-selection-status').textContent,/Unsaved/);
assert.match(elements.get('plugin-results').textContent,/Showing 1 of 3/);
document.getElementById('plugin-search').value='nonexistent';
applyPluginCatalogView();
assert.equal(cards.filter(card=>!card.hidden).length,0);
assert.equal(elements.get('plugin-no-results').hidden,false);
const originalCards=new Set(cards);
resetPluginCatalogView();
assert.deepEqual(cards.map(card=>card.dataset.pluginCard),['browseragent','reports','workflows']);
assert.ok(cards.every(card=>originalCards.has(card)));
assert.equal(elements.get('plugin-no-results').hidden,true);
document.getElementById('plugin-search').value='Browser';
applyPluginCatalogView();
await savePluginSelection(elements.get('plugin-save-btn'));
assert.deepEqual(saved,{method:'PUT',path:'/admin/plugins',body:{installed:['reports','workflows'],enabled:['workflows']}});
assert.equal(reloaded,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_plugin_cards_display_pricing_separately_from_installation_status():
    source = (FRONTEND / "plugins.js").read_text(encoding="utf-8")
    run_node(r"""
const assert=require('node:assert/strict');
class Element {
 constructor(){this.children=[];this.dataset={};this.attributes={};}
 append(...children){this.children.push(...children);}
 replaceChildren(){this.children=[];}
 setAttribute(key,value){this.attributes[key]=value;}
 addEventListener(){}
}
const root=new Element();
const document={createElement:()=>new Element(),createTextNode:text=>({textContent:text}),
 getElementById:id=>id==='plugin-catalog'?root:null};
const ACTION_ICON_PATHS={settings:'M1 1'};
""" + source + r"""
updatePluginSelectionStatus=()=>{};
updatePluginDetailSelection=()=>{};
pluginDestination=()=>'';
_pluginCatalog=[
 {id:'free',name:'Free plugin',description:'',pricing:'free',available:true,installed:true,enabled:true},
 {id:'paid',name:'Paid plugin',description:'',pricing:'paid',available:true,installed:true,enabled:false},
 {id:'legacy',name:'Legacy plugin',description:'',available:true,installed:false,enabled:false},
 {id:'unknown',name:'Unknown pricing',description:'',pricing:'unrecognized',available:true,installed:false,enabled:false},
];
renderPluginCatalog();
const badges=root.children.map(card=>card.children[0].children[1].children);
assert.deepEqual(badges.map(pair=>pair[0].textContent),['Free','Paid','Free','Pricing unavailable']);
assert.match(badges[0][0].className,/plugin-price-free/);
assert.match(badges[1][0].className,/plugin-price-paid/);
assert.equal(badges[1][0].attributes['aria-label'],'Pricing: Paid');
assert.equal(badges[1][1].dataset.pluginStatus,'paid');
assert.equal(root.children[1].children[2].children[0].children[0].children[0].disabled,false);
assert.equal(root.children[1].children[2].children[1].children[0].disabled,false);
""")


def test_plugin_detail_status_switches_between_enabled_green_and_disabled_gray():
    source = (FRONTEND / "plugins.js").read_text(encoding="utf-8")
    run_node(r"""
const assert=require('node:assert/strict');
const elements=new Map(),_jwt='fixture',_me={role:'admin'};
const document={getElementById(id){
 if(id==='plugin-settings-form')return null;
 if(!elements.has(id))elements.set(id,{className:'pill pill-gray',replaceChildren(){}});
 return elements.get(id);
}};
async function api(){return {values:{}};}
function toast(message){throw new Error(message);}
""" + source + r"""
(async()=>{
decoratePluginButtons=()=>{};
updatePluginDetailSelection=()=>{};
renderPluginDetailTabs=()=>{};
_pluginCatalog=[{id:'state_fixture',name:'Fixture',description:'Status test',installed:true,enabled:true}];
await openPluginDetails('state_fixture',false);
const badge=document.getElementById('plugin-detail-state');
assert.equal(badge.textContent,'Enabled');
assert.equal(badge.className,'pill pill-green');
_pluginCatalog[0].enabled=false;
await openPluginDetails('state_fixture',false);
assert.match(badge.textContent,/Disabled/);
assert.equal(badge.className,'pill pill-gray');
_pluginCatalog[0].enabled=true;
await openPluginDetails('state_fixture',false);
assert.equal(badge.className,'pill pill-green');
assert.equal(document.getElementById('extension-plugin-workspace').hidden,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


def test_plugin_setting_tabs_preserve_hidden_drafts_and_support_keyboard_navigation():
    source = (FRONTEND / "plugins.js").read_text(encoding="utf-8")
    run_node(r"""
const assert=require('node:assert/strict');
const elements=new Map();
class Element {
 constructor(tag='div'){this.tag=tag;this.children=[];this.dataset={};this.attributes={};this.events={};}
 set id(value){this._id=value;elements.set(value,this);}
 get id(){return this._id;}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(){this.children=[];}
 setAttribute(key,value){this.attributes[key]=value;}
 removeAttribute(key){delete this.attributes[key];}
 addEventListener(name,fn){this.events[name]=fn;}
 focus(){this.focused=true;}
 querySelectorAll(selector){
  return this.children.flatMap(child=>[
   ...((selector==='[data-plugin-section]'?child.dataset.pluginSection:child.dataset.settingType)?[child]:[]),
   ...child.querySelectorAll(selector),
  ]);
 }
}
const document={createElement:tag=>new Element(tag),getElementById:id=>elements.get(id)||null};
for(const id of ['plugin-detail-content','plugin-detail-tabs','plugin-detail-monitor-slot','plugin-diagnostics-panel']){
 const element=new Element();element.id=id;
}
const ACTION_ICON_PATHS={settings:'M1 1',inspect:'M2 2',save:'M3 3'};
const errors=[];
function toast(message){errors.push(message);}
let saved;
async function api(method,path,body){saved={method,path,body};}
""" + source + r"""
(async()=>{
 _pluginDetailId='browseragent';
 renderPluginSettingsForm('browseragent',{
  target_url:'http://localhost:9876/command/batch',timeout_seconds:30,
  screenshot:true,console:true,metrics:true,dom_snapshot:false,
 });
 renderPluginDetailTabs();
 const form=elements.get('plugin-settings-form');
 const save=form.children.at(-1);
 assert.equal(save.disabled,true);
 elements.get('plugin-setting-timeout_seconds').value='71';
 form.events.input();
 assert.equal(save.disabled,false);
 elements.get('plugin-setting-timeout_seconds').value='30';
 form.events.change();
 assert.equal(save.disabled,true);
 assert.equal(elements.get('plugin-settings-status').textContent,'No unsaved changes');
 const fields=form.querySelectorAll('[data-setting-type]');
 assert.equal(fields.length,6);
 assert.equal(elements.get('plugin-detail-tab-connection').attributes['aria-selected'],'true');
 assert.equal(elements.get('plugin-detail-panel-capture').hidden,true);
 elements.get('plugin-setting-timeout_seconds').value='71';
 selectPluginDetailTab('capture');
 assert.equal(elements.get('plugin-detail-panel-connection').hidden,true);
 assert.equal(elements.get('plugin-detail-panel-capture').hidden,false);
 elements.get('plugin-setting-screenshot').checked=false;
 assert.equal(pluginDetailsDirty(),true);
 elements.get('plugin-detail-tab-capture').events.keydown({key:'ArrowRight',preventDefault(){}});
 assert.equal(elements.get('plugin-diagnostics-panel').hidden,false);
 assert.equal(elements.get('plugin-detail-content').hidden,true);
 assert.equal(elements.get('plugin-detail-tab-diagnostics').focused,true);
 elements.get('plugin-detail-tab-diagnostics').events.keydown({key:'Home',preventDefault(){}});
 assert.equal(elements.get('plugin-detail-content').hidden,false);
 assert.equal(elements.get('plugin-detail-tab-connection').tabIndex,0);
 assert.equal(elements.get('plugin-detail-tab-capture').tabIndex,-1);
 assert.equal(elements.get('plugin-settings-form'),form);
 assert.equal(elements.get('plugin-setting-timeout_seconds').value,'71');
 assert.equal(elements.get('plugin-setting-screenshot').checked,false);
 // A submit serializes every section, including the hidden capture controls.
 form.querySelectorAll=selector=>selector.includes('="json"')?[]:fields;
 await savePluginSettings(form,save);
 assert.equal(saved.path,'/admin/plugins/browseragent/settings');
 assert.equal(saved.body.timeout_seconds,71);
 assert.equal(saved.body.screenshot,false);
 assert.equal(pluginDetailsDirty(),false);
 assert.equal(save.disabled,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
