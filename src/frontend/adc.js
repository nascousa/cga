'use strict';

let adcState = null;
let adcFieldId = 0;

function adcElement(tag, text, parent) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (parent) parent.append(el);
  return el;
}

function adcField(parent, title, type = 'input') {
  const label = adcElement('label', title, parent);
  label.style.display = 'block';
  label.style.margin = '12px 0';
  const el = adcElement(type, undefined, parent);
  el.id = `adc-field-${++adcFieldId}`;
  label.htmlFor = el.id;
  el.style.display = 'block';
  el.style.width = '100%';
  if (type === 'textarea') { el.rows = 12; el.spellcheck = false; }
  return el;
}

function adcButton(parent, title, action) {
  const button = adcElement('button', title, parent);
  button.className = 'btn btn-sm';
  button.type = 'button';
  button.style.margin = '4px';
  button.addEventListener('click', () => adcAction(action));
  return button;
}

async function adcAction(action) {
  if (!adcState || adcState.busy) return;
  adcState.busy = true;
  adcState.error.textContent = '';
  try { await action(); }
  catch (error) {
    adcState.error.textContent = error.message;
    toast(error.message, 'error');
  } finally { adcState.busy = false; }
}

function adcOptions(select, entries) {
  select.replaceChildren();
  for (const [value, label] of entries) {
    const option = adcElement('option', label, select);
    option.value = value;
  }
}

function adcChangesPayload() {
  return adcState.changes.map(({path, kind, content, reason, expires_at}) => ({path, kind, content, reason, expires_at}));
}

async function loadAdcManager() {
  if (_me?.role !== 'admin') return;
  const root = document.getElementById('adc-manager');
  if (adcState) return;
  root.replaceChildren();
  adcElement('h2', 'ADC governance', root);
  adcElement('p', 'Immutable releases, pinned project baselines, and audited amendments / overrides / exemptions. New projects pin the latest published version; existing projects never auto-upgrade.', root);
  const error = adcElement('p', '', root);
  error.setAttribute('role', 'alert');
  error.style.color = 'var(--danger, #ff6b6b)';
  const layout = adcElement('div', undefined, root);
  layout.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:24px';
  const catalog = adcElement('section', undefined, layout);
  const project = adcElement('section', undefined, layout);
  adcElement('h3', 'ADC release catalog', catalog);
  const releaseSelect = adcField(catalog, 'Published release', 'select');
  const provenance = adcElement('p', '', catalog);
  const docSelect = adcField(catalog, 'Section / document', 'select');
  const docPath = adcField(catalog, 'Document path (.adc/... or .github/...)');
  const content = adcField(catalog, 'Content (staged locally until published as a new version)', 'textarea');
  const draftStatus = adcElement('p', 'Published content is immutable. Editing never changes an existing version.', catalog);
  const version = adcField(catalog, 'New release version (major.minor.patch)');
  const releaseReason = adcField(catalog, 'Publication reason');
  const importFile = adcField(catalog, 'Import release JSON (version, documents, reason, source)');
  importFile.type = 'file'; importFile.accept = '.json,application/json';
  adcState = {busy:false, error, releaseSelect, provenance, docSelect, docPath, content, draftStatus,
    version, releaseReason, documents:{}, releases:[], project, changes:[], dirty:false};
  adcButton(catalog, 'Stage document', () => {
    const path = docPath.value.trim();
    if (!path) throw new Error('Enter a document path.');
    adcState.documents[path] = content.value;
    adcState.dirty = true;
    adcRenderDocuments(path);
    draftStatus.textContent = 'Unpublished changes staged in this browser. Publish a NEW version to save them.';
  });
  adcButton(catalog, 'Remove document from draft', () => {
    if (!docSelect.value || !confirm('Remove this document from the unpublished draft?')) return;
    delete adcState.documents[docSelect.value];
    adcState.dirty = true;
    adcRenderDocuments();
    draftStatus.textContent = 'Unpublished removal staged; existing releases are unchanged.';
  });
  adcButton(catalog, 'Load JSON into draft', async () => {
    if (!importFile.files[0]) throw new Error('Choose a release JSON file.');
    if (importFile.files[0].size > 5_500_000) throw new Error('Release JSON is too large.');
    const data = JSON.parse(await importFile.files[0].text());
    if (!data.documents || typeof data.documents !== 'object' || Array.isArray(data.documents)
      || Object.values(data.documents).some(v => typeof v !== 'string')) throw new Error('documents must map paths to text.');
    adcState.documents = {...data.documents};
    adcState.draftSource = data.source || 'CGA administrator JSON import';
    version.value = data.version || '';
    releaseReason.value = data.reason || '';
    adcState.dirty = true;
    adcRenderDocuments();
    draftStatus.textContent = 'Imported into local draft only. Review all content before publishing.';
  });
  adcButton(catalog, 'Publish immutable version', async () => {
    if (content.value !== (adcState.documents[docPath.value] ?? '')) throw new Error('Stage the edited document before publishing.');
    if (!confirm('Publish these staged documents as an immutable ADC release?')) return;
    const created = await api('POST', '/adc/releases', {version:version.value.trim(), documents:adcState.documents,
      reason:releaseReason.value.trim(), source:adcState.draftSource || `Derived from ADC ${adcState.release?.version || 'catalog'}`});
    adcState.dirty = false;
    await adcLoadCatalog(created.id);
    toast('ADC release published. Existing project versions were not changed.');
  });
  adcButton(catalog, 'Export release JSON', () => {
    if (!adcState.release) throw new Error('Select a release.');
    const {version, documents, reason, source} = adcState.release;
    adcSaveBlob(new Blob([JSON.stringify({version, documents, reason, source}, null, 2)], {type:'application/json'}), `adc-${version}.json`);
  });
  releaseSelect.addEventListener('change', () => adcAction(async () => {
    if (adcState.dirty && !confirm('Discard the unpublished local draft?')) { releaseSelect.value = adcState.release.id; return; }
    await adcLoadRelease(releaseSelect.value);
  }));
  docSelect.addEventListener('change', () => adcShowDocument());
  content.addEventListener('input', () => {
    adcState.dirty = true;
    draftStatus.textContent = 'Editor has unstaged changes. Click Stage document before publishing.';
  });
  adcBuildProjectPanel(project);
  await adcAction(async () => {
    await adcLoadCatalog();
    const projects = await api('GET', '/auth/projects');
    adcOptions(adcState.projectSelect, [['', 'Select a project'], ...projects.filter(p=>p.is_active).map(p=>[p.id,p.project_name])]);
  });
}

function adcRenderDocuments(selected) {
  adcOptions(adcState.docSelect, Object.keys(adcState.documents).sort().map(path=>[path,path]));
  if (selected) adcState.docSelect.value = selected;
  adcShowDocument();
}

function adcShowDocument() {
  const path = adcState.docSelect.value;
  adcState.docPath.value = path;
  adcState.content.value = adcState.documents[path] ?? '';
}

async function adcLoadCatalog(selected) {
  adcState.releases = await api('GET', '/adc/releases');
  adcOptions(adcState.releaseSelect, adcState.releases.map(r=>[r.id,`${r.version} - ${r.source}`]));
  adcOptions(adcState.targetRelease, adcState.releases.map((r,i)=>[r.id,`${r.version}${i===0?' (latest published)':''}`]));
  if (selected) adcState.releaseSelect.value = selected;
  if (adcState.releaseSelect.value) await adcLoadRelease(adcState.releaseSelect.value);
  if (adcState.current?.release) adcState.targetRelease.value = adcState.current.release.id;
}

async function adcLoadRelease(id) {
  const release = await api('GET', `/adc/releases/${Number(id)}`);
  adcState.release = release;
  adcState.documents = {...release.documents};
  adcState.dirty = false;
  adcState.draftSource = '';
  adcState.provenance.textContent = `${release.source} | published by ${release.actor} at ${release.created_at} | SHA256 ${release.sha256}`;
  adcState.version.value = `${release.major}.${release.minor}.${release.patch + 1}`;
  adcState.releaseReason.value = '';
  adcState.draftStatus.textContent = 'Published content loaded. Stage edits, then publish a new version.';
  adcRenderDocuments();
}

function adcBuildProjectPanel(parent) {
  adcElement('h3', 'Project ADC versions and exceptions', parent);
  const projectSelect = adcField(parent, 'Project', 'select');
  const summary = adcElement('p', 'Choose a project to view its pinned ADC baseline.', parent);
  const targetRelease = adcField(parent, 'Target ADC baseline (explicit adoption / upgrade)', 'select');
  const diffOutput = adcElement('pre', '', parent);
  diffOutput.style.cssText = 'white-space:pre-wrap;max-height:260px;overflow:auto';
  const reviewLabel = adcElement('label', undefined, parent);
  const review = adcElement('input', undefined, reviewLabel);
  review.type = 'checkbox';
  reviewLabel.append(' I reviewed the displayed baseline differences and all affected project changes.');
  const changeSelect = adcField(parent, 'Staged project changes', 'select');
  const path = adcField(parent, 'Change document path');
  const kind = adcField(parent, 'Change type', 'select');
  adcOptions(kind, [['override','Override an existing baseline document'], ['amendment','Add a project-specific document'], ['exemption','Exempt a baseline document (retained in audit history)']]);
  const content = adcField(parent, 'Replacement / amendment content', 'textarea');
  const reason = adcField(parent, 'Justification for this change');
  const expires = adcField(parent, 'Exemption expiry (optional, local time)');
  expires.type = 'datetime-local';
  const revisionReason = adcField(parent, 'Reason for saving this project revision');
  const history = adcElement('div', undefined, parent);
  Object.assign(adcState, {projectSelect, summary, targetRelease, diffOutput, review, changeSelect,
    changePath:path, changeKind:kind, changeContent:content, changeReason:reason, changeExpires:expires, revisionReason, history});
  projectSelect.addEventListener('change', ()=>adcAction(async () => {
    if (adcState.projectDirty && !confirm('Discard staged project changes?')) {
      projectSelect.value=adcState.current?.project_id||'';
      return;
    }
    await adcLoadProject();
  }));
  targetRelease.addEventListener('change', ()=> { review.checked=false; adcState.diffReviewedTarget=null; diffOutput.textContent='Click Preview upgrade differences before applying.'; });
  kind.addEventListener('change', ()=> { content.disabled=kind.value==='exemption'; expires.disabled=kind.value!=='exemption'; });
  expires.disabled=true;
  changeSelect.addEventListener('change', adcSelectChange);
  adcButton(parent, 'Stage project change', () => {
    if (!adcState.current) throw new Error('Choose a project.');
    if (!reason.value.trim()) throw new Error('A justification is required.');
    const change = {path:path.value.trim(), kind:kind.value, content:kind.value==='exemption'?null:content.value,
      reason:reason.value.trim(), expires_at:kind.value==='exemption'&&expires.value?new Date(expires.value).toISOString():null};
    adcState.changes = adcChangesPayload().filter(c=>c.path!==change.path);
    adcState.changes.push(change);
    adcState.projectDirty=true;
    adcRenderChanges(change.path);
    summary.textContent += ' Unsaved changes staged.';
  });
  adcButton(parent, 'Remove selected change', () => {
    adcState.changes = adcChangesPayload().filter(c=>c.path!==changeSelect.value);
    adcState.projectDirty=true;
    adcRenderChanges();
  });
  adcButton(parent, 'Preview upgrade differences', async () => {
    if (!adcState.current) throw new Error('Choose a project.');
    if (!adcState.current.release) { diffOutput.textContent='First adoption: review the release catalog contents.'; adcState.diffReviewedTarget=Number(targetRelease.value); return; }
    const differences = await api('GET', `/adc/diff?from_release=${adcState.current.release.id}&to_release=${Number(targetRelease.value)}`);
    diffOutput.textContent = differences.length ? differences.map(d=>`${d.kind}: ${d.path}\n${d.diff}`).join('\n\n') : 'No baseline document differences.';
    adcState.diffReviewedTarget=Number(targetRelease.value);
    adcState.diffPaths=differences.map(d=>d.path);
  });
  adcButton(parent, 'Save new project revision', async () => {
    if (!adcState.current) throw new Error('Choose a project.');
    const target=Number(targetRelease.value);
    const switching=adcState.current.release && adcState.current.release.id!==target;
    if (switching && (!review.checked || adcState.diffReviewedTarget!==target)) throw new Error('Preview and acknowledge baseline differences before switching versions.');
    await api('POST', `/adc/projects/${adcState.current.project_id}/revisions`, {
      release_id:target, expected_revision:adcState.current.revision, reason:revisionReason.value.trim(),
      changes:adcChangesPayload(), reviewed_paths:switching?(adcState.diffPaths||[]):[],
    });
    await adcLoadProject();
    toast('New project revision saved. Previous revisions remain available.');
  });
  adcButton(parent, 'Download effective project ADC', async () => {
    if (!adcState.current?.release) throw new Error('Adopt a release first.');
    await adcDownload(adcState.current.project_id);
  });
  adcButton(parent, 'View effective documents', async () => {
    if (!adcState.current?.release) throw new Error('Adopt a release first.');
    const fresh=await api('GET', `/adc/projects/${adcState.current.project_id}`);
    diffOutput.textContent=JSON.stringify({evaluated_at:fresh.evaluated_at, changes:fresh.changes, documents:fresh.documents},null,2);
  });
}

function adcRenderChanges(selected) {
  adcOptions(adcState.changeSelect, [['','New change'], ...adcState.changes.map(c=>[c.path,`${c.kind}: ${c.path}${c.active===false?' (expired)':''}`])]);
  if (selected) adcState.changeSelect.value=selected;
  adcSelectChange();
}

function adcSelectChange() {
  const c=adcState.changes.find(c=>c.path===adcState.changeSelect.value);
  adcState.changePath.value=c?.path||'';
  adcState.changeKind.value=c?.kind||'override';
  adcState.changeContent.value=c?.content||'';
  adcState.changeContent.disabled=c?.kind==='exemption';
  adcState.changeReason.value=c?.reason||'';
  adcState.changeExpires.disabled=c?.kind!=='exemption';
  const date=c?.expires_at?new Date(c.expires_at):null;
  adcState.changeExpires.value=date?new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16):'';
}

async function adcLoadProject() {
  adcState.projectDirty=false;
  adcState.current=null;
  adcState.changes=[];
  adcState.history.replaceChildren();
  adcState.review.checked=false;
  adcState.diffReviewedTarget=null;
  adcState.diffOutput.textContent='';
  adcState.revisionReason.value='';
  adcRenderChanges();
  if (!adcState.projectSelect.value) { adcState.summary.textContent='Choose a project.'; return; }
  const id=Number(adcState.projectSelect.value);
  const state=await api('GET', `/adc/projects/${id}`);
  adcState.current=state;
  adcState.changes=state.changes;
  adcState.summary.textContent=state.release?`Pinned ADC ${state.release.version}, project revision ${state.revision}. Saved by ${state.actor} at ${state.created_at}.`:'Not yet adopted. Select a release and save the first revision; existing projects are not silently changed.';
  adcState.targetRelease.value=state.release?.id||adcState.releases[0]?.id||'';
  adcRenderChanges();
  const history=await api('GET', `/adc/projects/${id}/history`);
  adcElement('h4','Immutable project history',adcState.history);
  for (const entry of history) {
    const row=adcElement('div',undefined,adcState.history);
    adcElement('p',`r${entry.revision} | ADC ${entry.version} | ${entry.actor} | ${entry.created_at} | ${entry.reason}${entry.restored_from?` | restored from r${entry.restored_from}`:''}`,row);
    adcButton(row,'Inspect revision',async()=>{
      const historic=await api('GET',`/adc/projects/${id}?revision=${entry.revision}`);
      adcState.diffOutput.textContent=JSON.stringify(historic,null,2);
    });
    adcButton(row,'Download revision',()=>adcDownload(id,entry.revision));
    adcButton(row,'Restore as new revision',async()=>{
      const reason=prompt(`Restore r${entry.revision} as a new revision? Enter the reason. Expired exemptions remain expired.`);
      if (!reason) return;
      await api('POST',`/adc/projects/${id}/restore`,{revision:entry.revision,expected_revision:adcState.current.revision,reason});
      await adcLoadProject();
      toast('Rollback recorded as a new revision.');
    });
  }
}

function adcSaveBlob(blob, name) {
  const url=URL.createObjectURL(blob);
  const link=document.createElement('a');
  link.href=url; link.download=name;
  document.body.append(link); link.click(); link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}

async function adcDownload(projectId, revision) {
  const response=await fetch(`/api/adc/projects/${projectId}/download${revision?`?revision=${revision}`:''}`,{headers:{Authorization:`Bearer ${_jwt}`}});
  if (!response.ok) {
    const body=await response.json();
    throw new Error(body.detail||'ADC download failed');
  }
  adcSaveBlob(await response.blob(),`adc-project-${projectId}-${revision?`r${revision}`:'current'}.zip`);
}
