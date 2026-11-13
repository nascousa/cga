'use strict';

function adcBuildCollectionPanel(parent) {
  parent = adcDisclosure(parent, 'Collect project rules');
  adcState.collectionPanel = parent;
  adcInfoText(parent, 'Collect project rules', [
    ['Preview', 'Inspect every active registered checkout before collecting snapshots.'],
    ['Scope', 'Collect entry rules, tool pointers, standards/, rules/ and .adc/.github/ as project overrides of the pinned baseline.'],
    ['Local files', 'Source files remain distribution copies. Operational context, credentials and Relay state are excluded.'],
    ['Blocked projects', 'Missing paths and conflicts must be resolved explicitly.'],
  ]);
  adcState.collectionPreviewButton = adcButton(parent, 'Preview all project rules', adcPreviewCollections);
  adcState.collectionButton = adcButton(parent, 'Collect available rule snapshots', adcCollectSnapshots);
  adcState.collectionButton.disabled = true;
  adcState.collectionStatus = adcElement('p', 'Preview required before collection.', parent);
  adcState.collectionList = adcElement('div', undefined, parent);
}

function adcRenderCollections() {
  adcState.collectionList.replaceChildren();
  for (const item of adcState.collections) {
    const row = adcElement('div', undefined, adcState.collectionList);
    adcElement('strong', `${item.project_name}: ${item.status}`, row);
    adcElement('span', item.error || item.note ? ` - ${item.error || item.note}` :
      ` - ${item.document_count} rules; ${item.changed_paths.length} differences; source SHA256 ${item.snapshot_sha256}${item.revision?`; collected as r${item.revision}`:''}`, row);
    if (item.changed_paths?.length) {
      adcElement('p', 'Changed paths:', row);
      const paths = adcElement('ul', undefined, row);
      for (const path of item.changed_paths) adcElement('li', path, paths);
    }
    if (item.rule_review?.length) {
      const review = adcDisclosure(row, 'Review source vs effective rules / conflicts');
      for (const rule of item.rule_review) {
        adcElement('h4', rule.path, review);
        if (rule.conflict) adcElement('p', `Conflict: ${rule.conflict}. Resolve it in Amendments, then preview again.`, review);
        for (const [label, content] of [['Current effective rule (pinned baseline for unbound projects)', rule.effective],
          ['Source rule to collect', rule.source]]) {
          adcElement('p', label, review);
          const text = adcElement('pre', content === null ? '(Not present)' : content, review);
          text.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere;max-height:24em;overflow:auto';
        }
      }
    }
    if (item.status === 'ready') {
      const label = adcElement('label', undefined, row);
      const checkbox = adcElement('input', undefined, label);
      checkbox.type = 'checkbox';
      checkbox.checked = !!item.reviewed;
      checkbox.addEventListener('change', () => {
        item.reviewed = checkbox.checked;
        adcUpdateCollectionButton();
      });
      adcElement('span', ' I reviewed this snapshot, its changed paths, source vs effective rules and conflicts.', label);
    }
  }
  adcUpdateCollectionButton();
}

function adcUpdateCollectionButton() {
  const ready = (adcState.collections || []).filter(p => p.status === 'ready');
  adcState.collectionButton.disabled = !!adcState.collecting || !ready.length || ready.some(p => !p.reviewed);
  adcState.collectionPreviewButton.disabled = !!adcState.collecting;
}

async function adcPreviewCollections() {
  if (adcState.collecting) throw new Error('Wait for collection to finish before previewing again.');
  adcState.collections = null;
  adcUpdateCollectionButton();
  adcState.collectionList.replaceChildren();
  adcState.collectionStatus.textContent = 'Scanning registered checkout rule snapshots...';
  try {
    adcState.collections = await api('GET', '/adc/rules/preview');
    adcRenderCollections();
    const blocked = adcState.collections.filter(p => p.status === 'blocked').length;
    adcState.collectionStatus.textContent = `${adcState.collections.length} projects checked; ${blocked} blocked. Review and acknowledge every ready project: collection commits all ready snapshots, not only the selected project. Resolve exemptions explicitly in Amendments and preview again.`;
  } catch (error) {
    adcState.collectionStatus.textContent = `Preview failed: ${error.message}`;
    throw error;
  }
}

async function adcCollectSnapshots() {
  if (!adcState.collections) throw new Error('Preview project rules first.');
  if (adcHasProjectDraft()) throw new Error('Save or discard staged or unstaged project changes before collecting rules.');
  const ready = adcState.collections.filter(p => p.status === 'ready');
  if (!ready.length) throw new Error('No ready snapshots. Resolve blocked projects or preview again.');
  if (adcState.collecting || ready.some(p => !p.reviewed)) throw new Error('Review and acknowledge every ready project snapshot before collection.');
  if (!confirm(`Collect ${ready.length} project rule snapshots into immutable CGA revisions? Existing baselines and local files will not be overwritten.`)) return;
  const reason = prompt('Justification for centralizing these project rules:');
  if (!reason?.trim()) throw new Error('A collection justification is required.');
  let saved = 0, failed = 0;
  adcState.collecting = true;
  adcUpdateCollectionButton();
  for (const item of ready) {
    try {
      const result = await api('POST', `/adc/projects/${item.project_id}/rules/collect`, {
        release_id:item.release_id, expected_revision:item.expected_revision,
        snapshot_sha256:item.snapshot_sha256, reason:reason.trim(),
      });
      item.status = 'managed';
      item.revision = result.revision;
      saved++;
    } catch (error) {
      item.status = 'blocked';
      item.error = error.message;
      failed++;
    }
    adcRenderCollections();
  }
  adcState.collecting = false;
  adcUpdateCollectionButton();
  const blocked = adcState.collections.filter(p => p.status === 'blocked').length;
  adcState.collectionStatus.textContent = `${saved} snapshots committed; ${failed} collection failures; ${blocked} projects blocked overall. Local files were not deleted.`;
  adcState.versionsStale = true;
  if (adcState.projectSelect.value) await adcLoadProject();
  if (blocked) throw new Error(`Collection is incomplete: ${blocked} projects remain blocked. See each project result.`);
  toast(`${saved} project rule snapshots collected into CGA.`);
}
