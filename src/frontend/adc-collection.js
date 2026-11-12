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
  adcButton(parent, 'Preview all project rules', adcPreviewCollections);
  adcButton(parent, 'Collect available rule snapshots', adcCollectSnapshots);
  adcState.collectionStatus = adcElement('p', 'Preview required before collection.', parent);
  adcState.collectionList = adcElement('div', undefined, parent);
}

function adcRenderCollections() {
  adcState.collectionList.replaceChildren();
  for (const item of adcState.collections) {
    const row = adcElement('p', undefined, adcState.collectionList);
    adcElement('strong', `${item.project_name}: ${item.status}`, row);
    adcElement('span', item.error || item.note ? ` - ${item.error || item.note}` :
      ` - ${item.document_count} rules; ${item.changed_paths.length} differences; source SHA256 ${item.snapshot_sha256}${item.revision?`; collected as r${item.revision}`:''}`, row);
  }
}

async function adcPreviewCollections() {
  adcState.collections = null;
  adcState.collectionList.replaceChildren();
  adcState.collectionStatus.textContent = 'Scanning registered checkout rule snapshots...';
  try {
    adcState.collections = await api('GET', '/adc/rules/preview');
    adcRenderCollections();
    const blocked = adcState.collections.filter(p => p.status === 'blocked').length;
    adcState.collectionStatus.textContent = `${adcState.collections.length} projects checked; ${blocked} blocked. Review differences and register missing repository paths before claiming complete collection.`;
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
  if (!confirm(`Collect ${ready.length} project rule snapshots into immutable CGA revisions? Existing baselines and local files will not be overwritten.`)) return;
  const reason = prompt('Justification for centralizing these project rules:');
  if (!reason?.trim()) throw new Error('A collection justification is required.');
  let saved = 0, failed = 0;
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
  const blocked = adcState.collections.filter(p => p.status === 'blocked').length;
  adcState.collectionStatus.textContent = `${saved} snapshots committed; ${failed} collection failures; ${blocked} projects blocked overall. Local files were not deleted.`;
  adcState.versionsStale = true;
  if (adcState.projectSelect.value) await adcLoadProject();
  if (blocked) throw new Error(`Collection is incomplete: ${blocked} projects remain blocked. See each project result.`);
  toast(`${saved} project rule snapshots collected into CGA.`);
}
