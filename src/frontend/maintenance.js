'use strict';

let _retentionSaved = null;
let _retentionPreview = null;
let _updateInfo = null;
let _updateReview = null;
let _updateTimer = null;
let _updateCheckId = 0;

function retentionDraft() {
  const value = id => Number(document.getElementById(id).value);
  return {
    enabled: document.getElementById('retention-enabled').checked,
    polling_days: value('retention-polling'), request_days: value('retention-request'),
    protected_days: value('retention-protected'), batch_size: value('retention-batch'),
  };
}

function retentionChanged() {
  _retentionPreview = null;
  document.getElementById('retention-preview-result').textContent = 'Preview this policy before saving.';
  retentionButtons();
}

function retentionButtons() {
  const changed = _retentionSaved && JSON.stringify(retentionDraft()) !== JSON.stringify(_retentionSaved);
  document.getElementById('retention-save').disabled = !changed || !_retentionPreview;
  document.getElementById('retention-run').disabled = !_retentionSaved?.enabled || Boolean(changed);
}

async function loadAuditRetention() {
  const status = document.getElementById('retention-status');
  status.textContent = 'Loading audit capacity and policy...';
  try {
    const data = await api('GET', '/admin/audit-retention');
    _retentionSaved = data.policy;
    _retentionPreview = null;
    document.getElementById('retention-enabled').checked = data.policy.enabled;
    for (const [field, id] of Object.entries({
      polling_days: 'polling', request_days: 'request', protected_days: 'protected', batch_size: 'batch',
    })) document.getElementById(`retention-${id}`).value = data.policy[field];
    const storage = data.storage;
    status.textContent = `${Number(storage.rows).toLocaleString()} records · ${_fmtBytes(storage.total_bytes)} total `
      + `(${_fmtBytes(storage.data_bytes)} data / ${_fmtBytes(storage.index_bytes)} indexes) · oldest ${storage.oldest || 'none'}.`;
    const run = data.last_run;
    document.getElementById('retention-last-run').textContent = run.finished_at
      ? `${_fmtIso(run.finished_at)} · ${run.status} · ${run.deleted} deleted${run.error ? ' · ' + run.error : ''}`
      : 'Not run. Cleanup is disabled until explicitly enabled. Enabled policies are checked every minute and run at most hourly.';
    document.getElementById('retention-preview-result').textContent = 'Preview is read-only. Backup retention does not control audit rows.';
    retentionButtons();
  } catch (error) {
    status.textContent = `Unable to load audit retention: ${error.message}`;
    toast(error.message, 'error');
  }
}

async function previewAuditRetention() {
  try {
    const draft = retentionDraft();
    const data = await api('POST', '/admin/audit-retention/preview', draft);
    _retentionPreview = {token: data.preview_token, draft: JSON.stringify(draft)};
    document.getElementById('retention-preview-result').textContent =
      `Eligible: ${Object.entries(data.counts).map(([kind, count]) => `${kind}: ${count.toLocaleString()}`).join(' · ') || '0 records'}. `
      + 'No data deleted. Preview expires in 15 minutes; unknown timestamp formats are preserved.';
    retentionButtons();
  } catch (error) {
    _retentionPreview = null;
    retentionButtons();
    toast(error.message, 'error');
  }
}

async function saveAuditRetention() {
  const draft = retentionDraft();
  if (!_retentionPreview || _retentionPreview.draft !== JSON.stringify(draft)) {
    toast('Preview the current policy first.', 'error');
    return;
  }
  if (!confirm(draft.enabled
    ? 'Enable this retention policy? Eligible audit records will be permanently deleted in bounded background batches. Review/archive required evidence before enabling.'
    : 'Save this policy with automatic deletion disabled?')) return;
  try {
    await api('PUT', '/admin/audit-retention', {policy: draft, preview_token: _retentionPreview.token});
    toast('Audit retention policy saved');
    await loadAuditRetention();
  } catch (error) { toast(error.message, 'error'); }
}

async function runAuditRetention() {
  if (!confirm('Run up to five cleanup batches under the saved policy? Deleted records cannot be recovered without a separate backup.')) return;
  const button = document.getElementById('retention-run');
  button.disabled = true;
  try {
    const result = await api('POST', '/admin/audit-retention/run', {confirm: true});
    toast(`Audit cleanup: ${result.status}; ${result.deleted} deleted`);
    await loadAuditRetention();
  } catch (error) { toast(error.message, 'error'); }
  finally { retentionButtons(); }
}

async function loadChangeLogs() {
  const root = document.getElementById('change-logs-list');
  root.textContent = 'Loading version history...';
  try {
    const data = await api('GET', '/change-logs');
    root.replaceChildren();
    const releases = data.unreleased.changes && Object.keys(data.unreleased.changes).length
      ? [data.unreleased, ...data.releases] : data.releases;
    for (const release of releases) {
      const details = document.createElement('details');
      details.className = 'settings-card';
      details.style.marginBottom = '10px';
      details.open = release.version === data.current_version;
      const summary = document.createElement('summary');
      summary.style.cursor = 'pointer';
      const state = release.version === 'Unreleased' ? 'Not released or installed'
        : release.version === data.current_version ? 'Current product version; local build may differ' : release.date;
      summary.textContent = `${release.version} · ${release.summary} · ${state}`;
      details.append(summary);
      for (const [category, entries] of Object.entries(release.changes)) {
        const heading = document.createElement('h3');
        heading.textContent = category;
        const list = document.createElement('ul');
        for (const entry of entries) {
          const item = document.createElement('li');
          item.textContent = entry;
          list.append(item);
        }
        details.append(heading, list);
      }
      root.append(details);
    }
    const note = document.createElement('p');
    note.className = 'settings-hint';
    note.textContent = data.history_note;
    root.append(note);
  } catch (error) {
    root.textContent = `Unable to load Change Logs: ${error.message}`;
    toast(error.message, 'error');
  }
}

function renderOnlineUpdate() {
  const data = _updateInfo;
  if (!data) return;
  const badge = document.getElementById('update-notice');
  badge.hidden = !data.available;
  badge.textContent = data.available ? `Update v${data.latest_version}` : '';
  badge.title = data.reason || 'Review changes before deciding whether to upgrade';
  const status = document.getElementById('online-update-status');
  const job = data.executor?.job;
  status.textContent = `Current ${data.current_version}; latest stable ${data.latest_version || 'unknown'}. `
    + `${data.reason || ''} ${data.executor?.reason || ''}`
    + (job?.message ? ` Host: ${job.status} · ${job.message}` : '')
    + ` Last check: ${_fmtIso(data.checked_at)}.`;
  document.getElementById('online-update-review').disabled = !data.available;
}

async function checkForUpdates(force = false) {
  clearTimeout(_updateTimer);
  if (!_jwt || !_me) return;
  const session = _jwt;
  const checkId = ++_updateCheckId;
  try {
    const data = await api(force ? 'POST' : 'GET', force ? '/admin/updates/check' : '/updates');
    if (_jwt !== session || checkId !== _updateCheckId) return;
    _updateInfo = data;
    renderOnlineUpdate();
  } catch (error) {
    if (_jwt !== session || checkId !== _updateCheckId) return;
    document.getElementById('online-update-status').textContent = `Update check failed: ${error.message}`;
    if (force) toast(error.message, 'error');
  } finally {
    if (_jwt === session && _me && checkId === _updateCheckId) _updateTimer = setTimeout(() => checkForUpdates(), 600000);
  }
}

function stopMaintenancePolling() {
  ++_updateCheckId;
  clearTimeout(_updateTimer);
  _updateTimer = null;
  _updateInfo = null;
  _updateReview = null;
  document.getElementById('update-notice').hidden = true;
}

function showUpdateDetails() {
  if (!_updateInfo?.available) return;
  const data = _updateInfo;
  _updateReview = data;
  document.getElementById('update-dialog-title').textContent = `Upgrade ${data.current_version} → ${data.latest_version}`;
  document.getElementById('update-dialog-notes').textContent = data.notes;
  document.getElementById('update-dialog-status').textContent = data.reason || data.executor?.reason
    || 'Confirmation starts a brief maintenance outage. The host updater verifies signatures, backs up data, replaces only this API image and rolls back if validation fails.';
  const button = document.getElementById('update-confirm');
  button.hidden = _me?.role !== 'admin';
  button.disabled = !data.installable || !data.executor?.ready || data.status !== 'ok';
  document.getElementById('update-dialog').showModal();
}

async function confirmOnlineUpdate() {
  const data = _updateReview;
  const session = _jwt;
  if (!data?.installable || !data.executor?.ready) return;
  if (!confirm(`Install v${data.latest_version}? CGA will briefly stop after draining work and making verified backups. Ports, configuration and data volumes are preserved.`)) return;
  const button = document.getElementById('update-confirm');
  button.disabled = true;
  try {
    const job = await api('POST', '/admin/updates/install', {
      confirm: true, version: data.latest_version, image: data.image,
    });
    if (_jwt !== session || !_me) return;
    document.getElementById('update-dialog-status').textContent = `Update ${job.id} queued. Waiting for the host executor. Keep this page open; status can also be recovered after reloading.`;
    await monitorOnlineUpdate(job.id);
  } catch (error) {
    document.getElementById('update-dialog-status').textContent = error.message;
    toast(error.message, 'error');
    await checkForUpdates();
  }
}

async function monitorOnlineUpdate(id) {
  const session = _jwt;
  for (let attempt = 0; attempt < 240 && _jwt === session && _me; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 5000));
    try {
      const data = await api('GET', '/updates');
      if (_jwt !== session || !_me) return;
      const job = data.executor?.job;
      if (job?.id !== id) continue;
      document.getElementById('update-dialog-status').textContent = `${job.status}: ${job.message}`;
      if (['success', 'rolled-back', 'failed', 'recovery-required'].includes(job.status)) {
        _updateInfo = data;
        renderOnlineUpdate();
        if (job.status === 'success') location.reload();
        return;
      }
    } catch (error) {
      document.getElementById('update-dialog-status').textContent =
        `Waiting for CGA to return on its original URL. Last response: ${error.message}`;
    }
  }
  document.getElementById('update-dialog-status').textContent =
    'Status monitoring ended. Inspect Upgrade status or host updater logs; do not assume completion or retry an active installation.';
}

async function cancelQueuedUpdate() {
  if (!confirm('Cancel only the queued, unclaimed update? An active host update cannot be interrupted here.')) return;
  try {
    await api('POST', '/admin/updates/cancel', {confirm: true});
    toast('Queued upgrade cancelled');
    await checkForUpdates();
  } catch (error) { toast(error.message, 'error'); }
}
