'use strict';

let adcState = null;
let adcFieldId = 0;
let adcInfoTooltip = null;

function adcElement(tag, text, parent) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (parent) parent.append(el);
  return el;
}

function adcHideInfo() {
  if (!adcInfoTooltip) return;
  clearTimeout(adcInfoTooltip.timer);
  adcInfoTooltip.root.hidden = true;
  adcInfoTooltip.button?.removeAttribute('aria-describedby');
  adcInfoTooltip.button = null;
  adcInfoTooltip.pinned = false;
  adcInfoTooltip.hovered = false;
}

function adcDisposeInfo() {
  if (!adcInfoTooltip) return;
  adcHideInfo();
  document.removeEventListener('pointerdown', adcInfoTooltip.outside);
  document.removeEventListener('keydown', adcInfoTooltip.escape);
  window.removeEventListener('resize', adcHideInfo);
  window.removeEventListener('scroll', adcInfoTooltip.scroll, true);
  adcInfoTooltip.root.remove();
  adcInfoTooltip = null;
}

function adcShowInfo(button, title, details) {
  if (!adcInfoTooltip) {
    const root = adcElement('div', undefined, document.body);
    root.id = 'adc-info-tooltip';
    root.setAttribute('role', 'tooltip');
    root.style.cssText = 'position:fixed;z-index:10000;box-sizing:border-box;width:min(360px,calc(100vw - 16px));max-height:calc(100dvh - 16px);overflow:auto;padding:12px;border:1px solid var(--border);border-radius:8px;background:var(--panel);color:var(--text);box-shadow:0 6px 24px #0005;font-size:13px;line-height:1.5;overflow-wrap:anywhere';
    const outside = event => {
      if (!root.contains(event.target) && !adcInfoTooltip.button?.contains(event.target)) adcHideInfo();
    };
    const escape = event => { if (event.key === 'Escape') adcHideInfo(); };
    const scroll = event => { if (!root.contains(event.target)) adcHideInfo(); };
    adcInfoTooltip = {root, outside, escape, scroll};
    root.addEventListener('pointerenter', () => {
      adcInfoTooltip.hovered = true;
      clearTimeout(adcInfoTooltip.timer);
    });
    root.addEventListener('pointerleave', () => {
      adcInfoTooltip.hovered = false;
      if (!adcInfoTooltip.pinned && document.activeElement !== adcInfoTooltip.button) adcHideInfo();
    });
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    window.addEventListener('resize', adcHideInfo);
    window.addEventListener('scroll', scroll, true);
  }
  adcHideInfo();
  const state = adcInfoTooltip;
  state.button = button;
  state.root.replaceChildren();
  adcElement('strong', title, state.root);
  const list = adcElement('ul', undefined, state.root);
  list.style.cssText = 'margin:8px 0 0;padding-left:18px';
  for (const [heading, text, format] of typeof details === 'function' ? details() : details) {
    const item = adcElement('li', undefined, list);
    adcElement('strong', `${heading}: `, item);
    const body = adcElement(format === 'code' ? 'code' : 'span', text, item);
    body.style.whiteSpace = 'pre-wrap';
  }
  state.root.hidden = false;
  button.setAttribute('aria-describedby', state.root.id);
  const anchor = button.getBoundingClientRect(), tip = state.root.getBoundingClientRect();
  state.root.style.left = `${Math.max(8, Math.min(anchor.left, document.documentElement.clientWidth - tip.width - 8))}px`;
  state.root.style.top = `${Math.max(8, Math.min(anchor.bottom + 6, window.innerHeight - tip.height - 8))}px`;
}

function adcInfoText(parent, title, details, tag = 'p', textTag = 'span') {
  const row = adcElement(tag, undefined, parent);
  row.className = 'adc-info-row';
  row.style.cssText = 'display:flex;align-items:center;gap:6px;min-width:0;margin:12px 0';
  const text = adcElement(textTag, title, row);
  text.style.cssText = 'min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
  const button = adcElement('button', undefined, row);
  button.type = 'button';
  button.className = 'adc-info-icon';
  button.setAttribute('aria-label', `More information: ${title}`);
  button.style.cssText = 'display:inline-flex;align-items:center;justify-content:center;flex:0 0 24px;width:24px;height:24px;padding:0;border:0;background:transparent;color:var(--muted);cursor:help';
  const glyph = adcElement('span', 'i', button);
  glyph.setAttribute('aria-hidden', 'true');
  glyph.style.cssText = 'display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box;width:14px;height:14px;border:1px solid currentColor;border-radius:50%;font:italic bold 10px Georgia,serif';
  const show = () => adcShowInfo(button, title, details);
  button.addEventListener('pointerenter', show);
  button.addEventListener('focus', show);
  button.addEventListener('pointerleave', () => {
    if (adcInfoTooltip?.button === button && !adcInfoTooltip.pinned && document.activeElement !== button) {
      adcInfoTooltip.timer = setTimeout(adcHideInfo, 180);
    }
  });
  button.addEventListener('blur', () => {
    if (adcInfoTooltip?.button === button && !adcInfoTooltip.pinned && !adcInfoTooltip.hovered) adcHideInfo();
  });
  button.addEventListener('click', () => {
    if (adcInfoTooltip?.button === button && adcInfoTooltip.pinned) adcHideInfo();
    else { show(); adcInfoTooltip.pinned = true; }
  });
  return text;
}

function adcField(parent, title, type = 'input', details) {
  const label = details ? adcInfoText(parent, title, details, 'div', 'label') : adcElement('label', title, parent);
  label.style.display = 'block';
  label.style.margin = details ? '0' : '12px 0';
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

function adcDisclosure(parent, title) {
  const details = adcElement('details', undefined, parent);
  details.className = 'adc-disclosure';
  const summary = adcElement('summary', undefined, details);
  summary.innerHTML = '<svg class="action-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="m9 5 7 7-7 7"></path></svg>';
  adcElement('span', title, summary);
  return details;
}

async function adcAction(action) {
  if (!adcState || adcState.busy) return;
  const state = adcState;
  state.busy = true;
  state.error.textContent = '';
  try { await action(); }
  catch (error) {
    if (adcState === state) {
      state.error.textContent = error.message;
      toast(error.message, 'error');
    }
  } finally { state.busy = false; }
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

function adcRuleEditorDirty() {
  return adcState.docPath.value !== (adcState.editorPath || '')
    || adcState.content.value !== (adcState.documents[adcState.editorPath] ?? '').replace(/\r\n?/g, '\n');
}

function adcProjectEditorValue() {
  return JSON.stringify([adcState.changePath.value, adcState.changeKind.value,
    adcState.changeContent.value, adcState.changeReason.value, adcState.changeExpires.value]);
}

function adcHasProjectDraft() {
  return adcState.projectDirty || adcProjectEditorValue() !== adcState.projectEditorValue
    || !!adcState.revisionReason.value.trim();
}

function adcHasRuleDraft() {
  return adcState.dirty || adcRuleEditorDirty();
}

async function loadAdcManager() {
  if (_me?.role !== 'admin') return;
  const root = document.getElementById('adc-manager');
  if (adcState) {
    if (adcState.activeTab === 'versions') adcState.chart?.resize();
    return;
  }
  root.replaceChildren();
  const header = adcElement('header', undefined, root);
  header.className = 'adc-header';
  adcElement('h2', 'ADC', header);
  adcInfoText(header, 'Agent Development Constitution', [
    ['Releases', 'Published rules are immutable; edits create a new release.'],
    ['Projects', 'New projects pin the latest release. Existing projects never auto-upgrade.'],
    ['Exceptions', 'Amendments, overrides and exemptions are audited as project revisions.'],
  ]);
  const error = adcElement('p', '', root);
  error.setAttribute('role', 'alert');
  error.style.color = 'var(--danger, #ff6b6b)';
  const layout = adcElement('div', undefined, root);
  const catalog = adcElement('section', undefined, layout);
  const project = adcElement('section', undefined, layout);
  const versions = adcElement('section', undefined, layout);
  const catalogToolbar = adcElement('div', undefined, catalog);
  catalogToolbar.className = 'adc-toolbar';
  const releaseGroup = adcElement('div', undefined, catalogToolbar);
  releaseGroup.className = 'adc-toolbar-field';
  const releaseSelect = adcField(releaseGroup, 'Published release', 'select');
  const provenance = adcInfoText(catalogToolbar, 'Release provenance', () => [
    ['Source', adcState.release.source], ['Publisher', adcState.release.actor],
    ['Published', adcState.release.created_at], ['SHA256', adcState.release.sha256, 'code'],
  ]);
  const workspace = adcElement('div', undefined, catalog);
  workspace.className = 'adc-constitution-workspace';
  const sidebar = adcElement('aside', undefined, workspace);
  sidebar.className = 'adc-document-sidebar';
  adcElement('h4', 'Documents', sidebar);
  const documentSearch = adcField(sidebar, 'Find a document');
  documentSearch.type = 'search';
  documentSearch.placeholder = 'Path or filename';
  const documentCount = adcElement('p', '', sidebar);
  documentCount.setAttribute('role', 'status');
  const documentList = adcElement('nav', undefined, sidebar);
  documentList.className = 'adc-document-list';
  documentList.setAttribute('aria-label', 'Constitution documents');
  const editor = adcElement('div', undefined, workspace);
  editor.className = 'adc-constitution-editor';
  const editorToolbar = adcElement('div', undefined, editor);
  editorToolbar.className = 'adc-toolbar';
  const pathGroup = adcElement('div', undefined, editorToolbar);
  pathGroup.className = 'adc-toolbar-field';
  const docPath = adcField(pathGroup, 'Document path (.adc/... or .github/...)');
  const documentActions = adcElement('div', undefined, editorToolbar);
  documentActions.className = 'adc-actions';
  const content = adcField(editor, 'Content', 'textarea', [
    ['Draft', 'Edits stay in this browser until staged and published as a new immutable release.'],
    ['History', 'Editing never changes an existing published version.'],
  ]);
  const draftStatus = adcElement('p', 'Published content is read-only history.', editor);
  const publication = adcDisclosure(editor, 'Publish & import');
  publication.className = 'adc-publication adc-disclosure';
  const releaseFields = adcElement('div', undefined, publication);
  releaseFields.className = 'adc-field-grid';
  const version = adcField(adcElement('div', undefined, releaseFields), 'New release version (major.minor.patch)');
  const releaseReason = adcField(adcElement('div', undefined, releaseFields), 'Publication reason');
  const importTools = adcDisclosure(publication, 'Import release JSON');
  const importFile = adcField(importTools, 'Release JSON file', 'input', [
    ['Fields', 'Provide version, documents, reason and source. Importing only loads a local draft.'],
    ['Review', 'Review the complete document set before publishing.'],
  ]);
  importFile.type = 'file'; importFile.accept = '.json,application/json';
  adcState = {busy:false, error, releaseSelect, provenance, documentSearch, documentList, documentCount, docPath, content, draftStatus, documentActions,
    version, releaseReason, documents:{}, releases:[], project, changes:[], dirty:false, disposeInfo:adcDisposeInfo};
  adcBuildNavigation(root, layout, {rules:catalog, projects:project, versions});
  adcBuildVersionsPanel(versions);
  adcButton(documentActions, 'Stage document', () => {
    const path = docPath.value.trim();
    if (!path) throw new Error('Enter a document path.');
    adcState.documents[path] = content.value;
    adcState.dirty = true;
    adcRenderDocuments(path);
    draftStatus.textContent = 'Unpublished changes staged in this browser. Publish a NEW version to save them.';
  });
  adcButton(documentActions, 'Remove document from draft', () => {
    if (!adcState.editorPath || !confirm('Remove this document from the unpublished draft?')) return;
    delete adcState.documents[adcState.editorPath];
    adcState.dirty = true;
    adcRenderDocuments();
    draftStatus.textContent = 'Unpublished removal staged; existing releases are unchanged.';
  });
  adcButton(importTools, 'Load JSON into draft', async () => {
    if (!importFile.files[0]) throw new Error('Choose a release JSON file.');
    if (importFile.files[0].size > 5_500_000) throw new Error('Release JSON is too large.');
    const data = JSON.parse(await importFile.files[0].text());
    if (!data.documents || typeof data.documents !== 'object' || Array.isArray(data.documents)
      || Object.values(data.documents).some(v => typeof v !== 'string')) throw new Error('documents must map paths to text.');
    if (adcHasRuleDraft() && !confirm('Replace the unpublished rule draft with this import?')) return;
    adcState.documents = {...data.documents};
    adcState.draftSource = data.source || 'CGA administrator JSON import';
    version.value = data.version || '';
    releaseReason.value = data.reason || '';
    adcState.dirty = true;
    adcRenderDocuments();
    publication.open = true;
    draftStatus.textContent = 'Imported into local draft only. Review all content before publishing.';
  });
  adcButton(publication, 'Publish immutable version', async () => {
    if (adcRuleEditorDirty()) throw new Error('Stage the edited document before publishing.');
    if (!confirm('Publish these staged documents as an immutable ADC release?')) return;
    const created = await api('POST', '/adc/releases', {version:version.value.trim(), documents:adcState.documents,
      reason:releaseReason.value.trim(), source:adcState.draftSource || `Derived from ADC ${adcState.release?.version || 'catalog'}`});
    adcState.dirty = false;
    await adcLoadCatalog(created.id);
    toast('ADC Constitution published. Existing project revisions were not changed.');
  });
  adcButton(publication, 'Export release JSON', () => {
    if (!adcState.release) throw new Error('Select a release.');
    const {version, documents, reason, source} = adcState.release;
    adcSaveBlob(new Blob([JSON.stringify({version, documents, reason, source}, null, 2)], {type:'application/json'}), `ADC-Release-v${version}-en.json`);
  });
  releaseSelect.addEventListener('change', () => adcAction(async () => {
    if (adcHasRuleDraft() && !confirm('Discard the unpublished local draft?')) { releaseSelect.value = adcState.release.id; return; }
    await adcLoadRelease(releaseSelect.value);
  }));
  documentSearch.addEventListener('input', adcRenderDocumentMenu);
  content.addEventListener('input', () => {
    adcState.dirty = true;
    draftStatus.textContent = 'Editor has unstaged changes. Click Stage document before publishing.';
  });
  adcBuildProjectPanel(project);
  adcBuildCollectionPanel(project);
  adcState.beforeUnload = event => {
    if (adcHasRuleDraft() || adcHasProjectDraft()) {
      event.preventDefault();
      event.returnValue = '';
    }
  };
  window.addEventListener('beforeunload', adcState.beforeUnload);
  await adcAction(async () => {
    await adcLoadCatalog();
    const projects = await api('GET', '/auth/projects');
    adcOptions(adcState.projectSelect, [['', 'Select a project'], ...projects.filter(p=>p.is_active).map(p=>[p.id,p.project_name])]);
  });
}

function adcRenderDocuments(selected) {
  const paths = Object.keys(adcState.documents).sort();
  adcShowDocument(paths.includes(selected) ? selected : paths[0] || '');
  adcRenderDocumentMenu();
}

function adcRenderDocumentMenu() {
  const paths = Object.keys(adcState.documents).sort();
  const query = adcState.documentSearch.value.trim().toLocaleLowerCase();
  const matches = paths.filter(path => path.toLocaleLowerCase().includes(query));
  adcState.documentCount.textContent = `${matches.length} of ${paths.length} documents`;
  const list = adcState.documentList;
  list.replaceChildren();
  if (!matches.length) {
    adcElement('p', paths.length ? 'No documents match this filter.' : 'No documents in this draft.', list);
  }
  for (const path of matches) {
    const button = adcElement('button', undefined, list);
    button.type = 'button';
    button.className = 'adc-document-link';
    button.setAttribute('data-adc-document', path);
    button.setAttribute('aria-label', path);
    if (path === adcState.editorPath) button.setAttribute('aria-current', 'page');
    button.innerHTML = '<svg class="action-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M8 13h8 M8 17h6"></path></svg>';
    const label = adcElement('span', undefined, button);
    const parts = path.split('/');
    adcElement('span', parts.pop(), label);
    adcElement('small', parts.join('/'), label);
    button.addEventListener('click', () => adcAction(() => {
      if (path === adcState.editorPath) return;
      if (adcRuleEditorDirty() && !confirm('Discard unstaged document edits? Staged documents will be retained.')) return;
      adcShowDocument(path);
    }));
  }
}

function adcShowDocument(path) {
  adcState.editorPath = path;
  adcState.docPath.value = path;
  adcState.content.value = adcState.documents[path] ?? '';
  for (const button of adcState.documentList.children) {
    if (button.getAttribute('data-adc-document') === path) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
}

async function adcLoadCatalog(selected) {
  adcState.releases = await api('GET', '/adc/releases');
  adcOptions(adcState.releaseSelect, adcState.releases.map(r=>[r.id,`ADC ${r.version}`]));
  adcOptions(adcState.targetRelease, adcState.releases.map((r,i)=>[r.id,`${r.version}${i===0?' (latest published)':''}`]));
  if (selected) adcState.releaseSelect.value = selected;
  if (adcState.releaseSelect.value) await adcLoadRelease(adcState.releaseSelect.value);
  if (adcState.current?.release) adcState.targetRelease.value = adcState.current.release.id;
  adcState.versionsStale = true;
}

async function adcLoadRelease(id) {
  const release = await api('GET', `/adc/releases/${Number(id)}`);
  adcState.release = release;
  adcState.documents = {...release.documents};
  adcState.dirty = false;
  adcState.draftSource = '';
  adcState.provenance.textContent = `ADC ${release.version} provenance`;
  adcState.version.value = `${release.major}.${release.minor}.${release.patch + 1}`;
  adcState.releaseReason.value = '';
  adcState.draftStatus.textContent = 'Published content loaded. Stage edits, then publish a new version.';
  adcRenderDocuments();
}

function adcBuildProjectPanel(parent) {
  const toolbar = adcElement('div', undefined, parent);
  toolbar.className = 'adc-toolbar';
  const projectGroup = adcElement('div', undefined, toolbar);
  projectGroup.className = 'adc-toolbar-field';
  const projectSelect = adcField(projectGroup, 'Project', 'select');
  const summary = adcInfoText(toolbar, 'Choose a project.', () => adcState.current?.release ? [
    ['Baseline', `ADC ${adcState.current.release.version}; project revision ${adcState.current.revision}.`],
    ['Saved by', adcState.current.actor], ['Saved at', adcState.current.created_at],
  ] : [['Adoption', 'Select a release and save the first revision. Existing projects are never silently changed.']]);
  const workspace = adcElement('div', undefined, parent);
  workspace.className = 'adc-amendment-workspace';
  const sidebar = adcElement('aside', undefined, workspace);
  sidebar.className = 'adc-amendment-sidebar';
  const changeSelect = adcField(sidebar, 'Project changes', 'select');
  changeSelect.size = 8;
  const baselinePanel = adcDisclosure(sidebar, 'Baseline & review');
  const targetRelease = adcField(baselinePanel, 'Target ADC baseline', 'select', [
    ['Adoption', 'Choosing a baseline does not save it. Save a new project revision to adopt or upgrade.'],
    ['Review', 'Preview and acknowledge differences before changing an existing baseline.'],
  ]);
  const diffOutput = adcElement('pre', '', baselinePanel);
  diffOutput.style.cssText = 'white-space:pre-wrap;max-height:260px;overflow:auto';
  const reviewLabel = adcElement('label', undefined, baselinePanel);
  const review = adcElement('input', undefined, reviewLabel);
  review.type = 'checkbox';
  reviewLabel.append(' I reviewed the displayed baseline differences and all affected project changes.');
  const editor = adcElement('div', undefined, workspace);
  editor.className = 'adc-amendment-editor';
  const changeFields = adcElement('div', undefined, editor);
  changeFields.className = 'adc-field-grid';
  const path = adcField(adcElement('div', undefined, changeFields), 'Change document path');
  const kind = adcField(adcElement('div', undefined, changeFields), 'Change type', 'select');
  adcOptions(kind, [['override','Override an existing baseline document'], ['amendment','Add a project-specific document'], ['exemption','Exempt a baseline document (retained in audit history)']]);
  const content = adcField(editor, 'Replacement / amendment content', 'textarea');
  const metadata = adcElement('div', undefined, editor);
  metadata.className = 'adc-field-grid';
  const reason = adcField(adcElement('div', undefined, metadata), 'Justification for this change');
  const expires = adcField(adcElement('div', undefined, metadata), 'Exemption expiry (optional, local time)');
  expires.type = 'datetime-local';
  const changeActions = adcElement('div', undefined, editor);
  changeActions.className = 'adc-actions';
  const commit = adcElement('div', undefined, editor);
  commit.className = 'adc-commit adc-toolbar';
  const commitGroup = adcElement('div', undefined, commit);
  commitGroup.className = 'adc-toolbar-field';
  const revisionReason = adcField(commitGroup, 'Reason for saving this project revision');
  const historyPanel = adcDisclosure(parent, 'Revision history');
  const history = adcElement('div', undefined, historyPanel);
  Object.assign(adcState, {projectSelect, summary, targetRelease, diffOutput, review, changeSelect,
    changePath:path, changeKind:kind, changeContent:content, changeReason:reason, changeExpires:expires, revisionReason, history, baselinePanel, historyPanel});
  projectSelect.addEventListener('change', ()=>adcAction(async () => {
    if (adcHasProjectDraft() && !confirm('Discard staged or unstaged project changes?')) {
      projectSelect.value=adcState.current?.project_id||'';
      return;
    }
    await adcLoadProject();
  }));
  targetRelease.addEventListener('change', ()=> { review.checked=false; adcState.diffReviewedTarget=null; diffOutput.textContent='Click Preview upgrade differences before applying.'; });
  kind.addEventListener('change', ()=> { content.disabled=kind.value==='exemption'; expires.disabled=kind.value!=='exemption'; });
  expires.disabled=true;
  changeSelect.addEventListener('change', () => {
    if (adcProjectEditorValue() !== adcState.projectEditorValue && !confirm('Discard unstaged project change edits?')) {
      changeSelect.value = adcState.projectEditorPath || '';
      return;
    }
    adcSelectChange();
  });
  adcButton(changeActions, 'Stage project change', () => {
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
  adcButton(changeActions, 'Remove selected change', () => {
    adcState.changes = adcChangesPayload().filter(c=>c.path!==changeSelect.value);
    adcState.projectDirty=true;
    adcRenderChanges();
  });
  adcButton(baselinePanel, 'Preview upgrade differences', async () => {
    if (!adcState.current) throw new Error('Choose a project.');
    if (!adcState.current.release) { diffOutput.textContent='First adoption: review the release catalog contents.'; adcState.diffReviewedTarget=Number(targetRelease.value); return; }
    const differences = await api('GET', `/adc/diff?from_release=${adcState.current.release.id}&to_release=${Number(targetRelease.value)}`);
    diffOutput.textContent = differences.length ? differences.map(d=>`${d.kind}: ${d.path}\n${d.diff}`).join('\n\n') : 'No baseline document differences.';
    adcState.diffReviewedTarget=Number(targetRelease.value);
    adcState.diffPaths=differences.map(d=>d.path);
  });
  adcButton(commit, 'Save new project revision', async () => {
    if (!adcState.current) throw new Error('Choose a project.');
    if (adcProjectEditorValue() !== adcState.projectEditorValue) throw new Error('Stage the edited project change before saving.');
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
  adcButton(toolbar, 'Download effective project ADC', async () => {
    if (!adcState.current?.release) throw new Error('Adopt a release first.');
    await adcDownload(adcState.current.project_id);
  });
  adcButton(baselinePanel, 'View effective documents', async () => {
    if (!adcState.current?.release) throw new Error('Adopt a release first.');
    const fresh=await api('GET', `/adc/projects/${adcState.current.project_id}`);
    baselinePanel.open = true;
    diffOutput.textContent=JSON.stringify({evaluated_at:fresh.evaluated_at, changes:fresh.changes, documents:fresh.documents},null,2);
  });
  adcRenderChanges();
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
  adcState.projectEditorPath = adcState.changeSelect.value;
  adcState.projectEditorValue = adcProjectEditorValue();
}

async function adcLoadProject() {
  adcState.versionsStale = true;
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
  adcState.summary.textContent=state.release?`ADC ${state.release.version} / revision ${state.revision}`:'No baseline adopted.';
  adcState.targetRelease.value=state.release?.id||adcState.releases[0]?.id||'';
  adcRenderChanges();
  const history=await api('GET', `/adc/projects/${id}/history`);
  adcElement('h4','Immutable project history',adcState.history);
  for (const entry of history) {
    const row=adcElement('div',undefined,adcState.history);
    adcInfoText(row, `r${entry.revision} / ADC ${entry.version}`, [
      ['Saved by', entry.actor], ['Saved at', entry.created_at], ['Reason', entry.reason],
      ['Restored from', entry.restored_from ? `r${entry.restored_from}` : 'Not a restoration.'],
    ]);
    adcButton(row,'Inspect revision',async()=>{
      const historic=await api('GET',`/adc/projects/${id}?revision=${entry.revision}`);
      adcState.baselinePanel.open = true;
      adcState.diffOutput.textContent=JSON.stringify(historic,null,2);
    });
    adcButton(row,'Download revision',()=>adcDownload(id,entry.revision));
    adcButton(row,'Restore as new revision',async()=>{
      if (adcHasProjectDraft() && !confirm('Discard staged or unstaged project changes before restoring?')) return;
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
