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
const buttons=['Dashboard','ADC','Report','Project'].map((name,i)=>{const b=new Element(name);b.id=['tab-briefing-btn','tab-adc-btn','tab-report-btn','tab-projects-btn'][i];return b;});
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
