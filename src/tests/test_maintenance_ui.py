from pathlib import Path
import shutil
import subprocess

import pytest


def test_retention_change_logs_and_update_session_guards():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for frontend execution tests")
    source = (Path(__file__).resolve().parents[1] / "frontend/maintenance.js").read_text(encoding="utf-8")
    script = r"""
const assert=require('node:assert/strict');
class Element {
 constructor(){this.value='';this.checked=false;this.children=[];this.textContent='';this.style={};}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=items;}
 showModal(){this.open=true;}
 set innerHTML(value){throw new Error('Untrusted HTML must not be rendered');}
}
const elements=new Map();
const document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement(){return new Element();}};
function _fmtBytes(n){return String(n);}
function _fmtIso(n){return String(n);}
function toast(message,type){if(type==='error')throw new Error(message);}
let _jwt='session',_me={role:'admin'};
let timerCount=0;
function setTimeout(){timerCount++;return timerCount;}
function clearTimeout(){}
let policy={enabled:false,polling_days:7,request_days:90,protected_days:365,batch_size:1000};
const calls=[];
let api=async(method,path,body)=>{
 calls.push({method,path,body});
 if(path==='/admin/audit-retention'&&method==='GET')return {policy:{...policy},storage:{rows:1000,total_bytes:5000,data_bytes:4000,index_bytes:1000},last_run:{}};
 if(path.endsWith('/preview'))return {counts:{request:100},preview_token:'test-preview'};
 if(path==='/admin/audit-retention'&&method==='PUT'){policy={...body.policy};return policy;}
 if(path==='/change-logs')return {current_version:'1.30.132',history_note:'Evidence-backed history only',unreleased:{version:'Unreleased',summary:'Pending',changes:{Added:['<img src=x onerror=attack()>']}},releases:[{version:'1.30.132',summary:'Installed',changes:{Fixed:['A real fix']}}]};
 throw new Error('Unexpected API '+path);
};
let accepted=false;
function confirm(){return accepted;}
(async()=>{
 await loadAuditRetention();
 assert.equal(elements.get('retention-save').disabled,true);
 assert.equal(elements.get('retention-run').disabled,true);
 elements.get('retention-enabled').checked=true;
 retentionChanged();
 assert.equal(elements.get('retention-save').disabled,true);
 await previewAuditRetention();
 assert.equal(elements.get('retention-save').disabled,false);
 await saveAuditRetention();
 assert.equal(calls.some(c=>c.method==='PUT'),false,'Cancellation must never save/enable deletion');
 accepted=true;
 await saveAuditRetention();
 assert.equal(policy.enabled,true);
 assert.equal(elements.get('retention-save').disabled,true);
 assert.equal(elements.get('retention-run').disabled,false);
 elements.get('retention-request').value='120';
 retentionChanged();
 assert.equal(elements.get('retention-save').disabled,true);
 assert.equal(elements.get('retention-run').disabled,true,'Never clean with an unsaved policy draft');
 await loadChangeLogs();
 const releases=elements.get('change-logs-list').children;
 assert.equal(releases[0].open,false);
 assert.equal(releases[1].open,true);
 assert.equal(releases[0].children[2].children[0].textContent,'<img src=x onerror=attack()>');
 _updateInfo={available:true,installable:true,status:'ok',current_version:'1.30.132',latest_version:'1.30.133',image:'reviewed-digest',executor:{ready:true},notes:'Reviewed notes'};
 showUpdateDetails();
 _updateInfo={..._updateInfo,latest_version:'1.30.134',image:'unreviewed-digest'};
 let install;
 api=async(method,path,body)=>{assert.equal(path,'/admin/updates/install');install=body;return{id:'test'};};
 monitorOnlineUpdate=async()=>{};
 await confirmOnlineUpdate();
 assert.equal(install.version,'1.30.133','A background refresh must not replace the target whose notes were reviewed');
 assert.equal(install.image,'reviewed-digest');
 let resolve;
 api=()=>new Promise(done=>{resolve=done;});
 const request=checkForUpdates();
 stopMaintenancePolling();_jwt='';
 resolve({available:true,latest_version:'9.0.0'});
 await request;
 assert.equal(_updateInfo,null,'An in-flight response must not restore logged-out update state');
 assert.equal(timerCount,0,'Logout must prevent timer resurrection');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node], input=source + script, text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
