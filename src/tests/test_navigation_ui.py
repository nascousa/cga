import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_navigation_collapses_for_actual_available_space_and_restores_labels():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for navigation layout tests")
    source = (Path(__file__).resolve().parents[1] / "frontend" / "navigation.js").read_text(encoding="utf-8")
    script = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(text='',width=0) { this.textContent=text; this.width=width; this.children=[]; this.attrs={}; this.listeners={}; this.style={}; this.hidden=false; this.compact=false; }
  get classList(){return {contains:name=>name==='is-compact'&&this.compact,toggle:(name,value)=>{this.compact=value;}};}
  getBoundingClientRect(){return {width:this.width,left:20,right:20+this.width,bottom:40};}
  getClientRects(){return this.hidden?[]:[this.getBoundingClientRect()];}
  setAttribute(name,value){this.attrs[name]=value;}
  getAttribute(name){return this.attrs[name];}
  removeAttribute(name){delete this.attrs[name];}
  addEventListener(name,fn){this.listeners[name]=fn;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  querySelector(selector){return selector==='.nav-label'?this.children.find(e=>e.className==='nav-label'):null;}
}
const nav=new Element(), bar=new Element(), brand=new Element('',220), user=new Element('',100);
const buttons=['Dashboard','ADC','Audit','Project'].map((name,i)=>{const b=new Element(name);b.id=['tab-briefing-btn','tab-adc-btn','tab-audit-btn','tab-projects-btn'][i];return b;});
nav.querySelectorAll=()=>buttons;
nav.closest=()=>bar;
bar.children=[brand,nav,user];
bar.clientWidth=720;
const body=new Element();
const document={querySelector:()=>nav,createElement:()=>new Element(),createElementNS:()=>new Element(),body,documentElement:{clientWidth:720},activeElement:null,fonts:{ready:Promise.resolve()}};
let scheduled;
const requestAnimationFrame=fn=>{scheduled=fn;return 1;};
const window={addEventListener(){}};
const ResizeObserver=class{observe(){}};
const MutationObserver=class{observe(){}};
function getComputedStyle(el){return {display:el.hidden?'none':'flex',paddingLeft:'12',paddingRight:'12',marginLeft:'0',marginRight:'0',columnGap:el===bar?'12':'2',borderLeftWidth:'1',borderRightWidth:'1',getPropertyValue:()=> '12'};}
"""
    assertions = r"""
function flush(){const fn=scheduled;scheduled=null;if(fn)fn();}
for(const button of buttons)button.querySelector('.nav-label').width=button.textContent.length*8;
flush();
assert.equal(nav.compact,false);
bar.clientWidth=560; updateTopbarNavigation();
assert.equal(nav.compact,true);
updateTopbarNavigation();
assert.equal(nav.compact,true);
bar.clientWidth=720; updateTopbarNavigation();
assert.equal(nav.compact,false);
buttons[0].hidden=true;buttons[2].hidden=true;
bar.clientWidth=550; updateTopbarNavigation();
assert.equal(nav.compact,false);
buttons[0].hidden=false;buttons[2].hidden=false;
updateTopbarNavigation();
assert.equal(nav.compact,true);
assert.equal(buttons[0].getAttribute('aria-label'),'Dashboard');
buttons[0].listeners.focus();
assert.equal(body.children[0].textContent,'Dashboard');
assert.equal(body.children[0].hidden,false);
buttons[0].listeners.keydown({key:'Escape'});
assert.equal(body.children[0].hidden,true);
document.activeElement=buttons[0];
buttons[0].listeners.focus();
nav.listeners.scroll();
assert.equal(body.children[0].hidden,false);
updateTopbarNavigation();
assert.equal(body.children[0].hidden,false);
bar.clientWidth=720;
updateTopbarNavigation();
assert.equal(body.children[0].hidden,true);
"""
    subprocess.run([node], input=script + source + assertions, check=True,
                   capture_output=True, text=True, timeout=15)


def test_audit_groups_sections_preserving_routes_permissions_and_polling():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for navigation layout tests")
    html = (Path(__file__).resolve().parents[1] / "frontend" / "index.html").read_text(encoding="utf-8")
    topbar = re.search(r'<div class="topbar-nav">(.*?)</div>', html, re.S).group(1)
    assert 'id="tab-report-btn"' not in topbar
    assert 'id="tab-ai-first-btn"' not in topbar
    names = (
        "enhanceAuditSections", "selectAuditSubtab", "normalizedAdminPath",
        "adminTabFromLocation", "adminRouteForTab", "canOpenAdminTab",
        "setAdminRoute", "switchTab",
    )
    functions = []
    for name in names:
        match = re.search(rf"function {name}\([^)]*\) \{{.*?^\}}", html, re.M | re.S)
        assert match is not None, name
        functions.append(match.group())
    constants = []
    for name in ("ADMIN_TAB_ROUTES", "ADMIN_PATH_TABS", "AUDIT_TAB_PANELS"):
        constants.append(re.search(
            rf"const {name} = Object\.freeze\(\{{.*?^\}}\);", html, re.M | re.S
        ).group())
    script = r"""
const assert=require('node:assert/strict');
class Element {
  constructor(id,classes=[]) {this.id=id;this.classes=new Set(classes);this.children=[];this.dataset={};this.attrs={};this.hidden=false;}
  get classList(){return {contains:n=>this.classes.has(n),add:n=>this.classes.add(n),remove:n=>this.classes.delete(n),toggle:(n,on)=>on?this.classes.add(n):this.classes.delete(n)};}
  append(child){this.children.push(child);child.parentElement=this;}
  setAttribute(n,v){this.attrs[n]=v;}
}
const elements=new Map();
for(const name of ['audit','ai-first','report','projects'])elements.set('pane-'+name,new Element('pane-'+name,['pane']));
for(const name of ['audit','projects'])elements.set('tab-'+name+'-btn',new Element('tab-'+name+'-btn',['tab-btn']));
for(const name of ['audit-events','app'])elements.set(name,new Element(name));
const subtabs=['audit','ai-first','report'].map(name=>{const el=new Element(name);el.dataset.auditTab=name;return el;});
const content=new Element('content');
const document={getElementById:id=>elements.get(id),querySelector:()=>content,querySelectorAll(selector){
  if(selector==='.pane')return [...elements.values()].filter(el=>el.classes.has('pane'));
  if(selector==='.tab-btn')return [...elements.values()].filter(el=>el.classes.has('tab-btn'));
  return subtabs;
}};
let _jwt='token',_me={role:'admin'},_auditPollInterval=null,_briefingPollInterval=null,_schedulePollInterval=null;
let nextTimer=0;const timers=new Set(),calls=[];
function setInterval(){timers.add(++nextTimer);return nextTimer;}
function clearInterval(id){timers.delete(id);}
function enhanceStaticBriefingSections(){}
function loadAudit(){calls.push('audit');}
function loadWorkReport(){calls.push('report');}
function loadAiFirstReadiness(){calls.push('ai-first');}
function loadAiFirstPolicyProfiles(){}
function loadAiFirstSignals(){}
function loadAiFirstEvidenceHistory(){}
const changes=[];
const window={location:new URL('http://localhost/admin/ai-first?evidence_id=example#evidence'),history:{}};
for(const action of ['replaceState','pushState'])window.history[action]=(state,unused,url)=>{changes.push(action);window.location=new URL(url,window.location);};
"""
    assertions = r"""
enhanceAuditSections();enhanceAuditSections();
assert.equal(elements.get('pane-audit').children.length,2);
assert.equal(elements.get('pane-report').parentElement,elements.get('pane-audit'));
switchTab('audit');
assert.equal(timers.size,1);
assert.equal(elements.get('audit-events').hidden,false);
window.location.pathname='/admin/ai-first';
switchTab(adminTabFromLocation(),null,{replace:true});
assert.equal(window.location.pathname,'/admin/audit/ai-first');
assert.equal(window.location.search,'?evidence_id=example');
assert.equal(window.location.hash,'#evidence');
assert.equal(timers.size,0);
assert.equal(elements.get('pane-audit').classes.has('active'),true);
assert.equal(elements.get('tab-audit-btn').classes.has('active'),true);
assert.equal(elements.get('pane-ai-first').hidden,false);
assert.equal(elements.get('audit-events').hidden,true);
assert.equal(subtabs[1].attrs['aria-pressed'],'true');
switchTab('report');
assert.equal(window.location.pathname,'/admin/audit/report');
assert.equal(elements.get('pane-report').hidden,false);
assert.equal(elements.get('pane-ai-first').hidden,true);
const historyLength=changes.length;
window.location.pathname='/admin/audit/ai-first';
switchTab(adminTabFromLocation(),null,{updateUrl:false});
assert.equal(changes.length,historyLength);
assert.equal(elements.get('pane-ai-first').hidden,false);
assert.deepEqual(calls,['audit','ai-first','report','ai-first']);
_me={role:'developer'};
switchTab('report');
assert.equal(window.location.pathname,'/admin/projects');
assert.equal(elements.get('pane-audit').classes.has('active'),false);
assert.equal(elements.get('pane-projects').classes.has('active'),true);
assert.equal(calls.length,4);
"""
    subprocess.run([node], input=script + "\n".join(constants + functions) + assertions,
                   check=True, capture_output=True, text=True, timeout=15)
