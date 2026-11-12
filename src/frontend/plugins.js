'use strict';

let _pluginCatalog = [];
let _pluginSelection = {installed: [], enabled: []};
const _pluginAssets = new Map();
const PLUGIN_TABS = Object.freeze({briefing: 'briefing', schedule: 'automation', report: 'reports'});
let _extensionView = 'plugins';
let _pluginDetailId = '';
let _pluginDetailRequest = 0;
let _pluginDetailSaved = '';
let _pluginMonitorDirty = false;
let _pluginRuntimeSettings = {};
let _pluginIconsObserver = null;
let _pluginDetailSections = [];
function pluginIcon(name) {
  const path = ACTION_ICON_PATHS[{reset: 'refresh', collapse: 'expand'}[name] || name];
  if (!path) throw new Error(`Unknown plugin action icon: ${name}`);
  return `<svg class="plugin-action-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="${path}"></path></svg>`;
}

function decoratePluginButtons() {
  const pane = document.getElementById('pane-extensions');
  if (!pane) return;
  for (const button of pane.querySelectorAll('button')) {
    if (button.querySelector('svg')) continue;
    const text = (button.textContent + ' ' + (button.getAttribute('onclick') || '')).toLowerCase();
    const name = button.dataset.pluginIcon || (text.includes('collapse') ? 'collapse'
      : text.includes('save') ? 'save' : text.includes('schedule') ? 'schedule'
      : text.includes('run') ? 'run' : text.includes('refresh') ? 'refresh'
      : text.includes('reset') ? 'reset' : text.includes('all plugins') ? 'back'
      : text.includes('check') ? 'inspect' : text.includes('open') ? 'open' : 'settings');
    button.insertAdjacentHTML('afterbegin', pluginIcon(name));
  }
  if (!_pluginIconsObserver) {
    _pluginIconsObserver = new MutationObserver(decoratePluginButtons);
    _pluginIconsObserver.observe(pane, {childList: true, subtree: true});
  }
}
const PLUGIN_CATEGORIES = Object.freeze({
  automation: 'Orchestration', browseragent: 'Browser testing', workflows: 'Integrations',
  azure_policy_change_monitor: 'Monitoring', briefing: 'Insights', reports: 'Reporting',
  notifications: 'Delivery', evaluation: 'Quality',
});

function pluginCategory(plugin) {
  return plugin.category || PLUGIN_CATEGORIES[plugin.id] || 'Plugin';
}

function pluginCatalogView(selection, filters) {
  const installed = new Set(selection.installed);
  const enabled = new Set(selection.enabled);
  const words = filters.search.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const compare = (left, right) => left.localeCompare(right, 'en', {sensitivity: 'base', numeric: true});
  const statusOrder = {'enabled': 0, 'disabled': 1, 'not-installed': 2, 'not-bundled': 3};
  return _pluginCatalog.map(plugin => ({
    ...plugin,
    category: pluginCategory(plugin),
    state: !plugin.available ? 'not-bundled' : enabled.has(plugin.id) ? 'enabled'
      : installed.has(plugin.id) ? 'disabled' : 'not-installed',
  })).filter(plugin => {
    const text = `${plugin.name} ${plugin.id} ${plugin.description} ${plugin.category}`.toLowerCase();
    return words.every(word => text.includes(word))
      && (!filters.category || plugin.category === filters.category)
      && (!filters.state || (filters.state === 'installed' ? installed.has(plugin.id) : plugin.state === filters.state));
  }).sort((left, right) => {
    const name = compare(left.name, right.name) || compare(left.id, right.id);
    if (filters.sort === 'name-desc') return -name;
    if (filters.sort === 'category') return compare(left.category, right.category) || name;
    if (filters.sort === 'status') return statusOrder[left.state] - statusOrder[right.state] || name;
    return name;
  });
}

function populatePluginCategoryFilter() {
  const select = document.getElementById('plugin-category-filter');
  if (!select) return;
  const selected = select.value;
  const categories = [...new Set(_pluginCatalog.map(pluginCategory))].sort((a, b) => a.localeCompare(b, 'en'));
  select.replaceChildren();
  for (const [value, label] of [['', 'All categories'], ...categories.map(category => [category, category])]) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    select.append(option);
  }
  select.value = categories.includes(selected) ? selected : '';
}

function applyPluginCatalogView(selection = readPluginSelection()) {
  const root = document.getElementById('plugin-catalog');
  if (!root) return;
  const entries = pluginCatalogView(selection, {
    search: document.getElementById('plugin-search')?.value || '',
    category: document.getElementById('plugin-category-filter')?.value || '',
    state: document.getElementById('plugin-state-filter')?.value || '',
    sort: document.getElementById('plugin-sort')?.value || 'name',
  });
  const cards = [...root.querySelectorAll('[data-plugin-card]')];
  const matches = new Set(entries.map(plugin => plugin.id));
  for (const card of cards) card.hidden = !matches.has(card.dataset.pluginCard);
  const visible = cards.filter(card => !card.hidden);
  if (visible.some((card, index) => card.dataset.pluginCard !== entries[index].id)) {
    const byId = new Map(cards.map(card => [card.dataset.pluginCard, card]));
    const focused = document.activeElement;
    root.append(...entries.map(plugin => byId.get(plugin.id)));
    if (focused && root.contains(focused)) focused.focus({preventScroll: true});
  }
  const results = document.getElementById('plugin-results');
  if (results) results.textContent = `Showing ${entries.length} of ${_pluginCatalog.length} plugins. Status filters include unsaved selections.`;
  const empty = document.getElementById('plugin-no-results');
  if (empty) empty.hidden = entries.length !== 0;
}

function resetPluginCatalogView() {
  for (const id of ['plugin-search', 'plugin-category-filter', 'plugin-state-filter']) {
    document.getElementById(id).value = '';
  }
  document.getElementById('plugin-sort').value = 'name';
  applyPluginCatalogView();
}

function switchExtensionView(view) {
  const monitorEnabled = _pluginSelection.installed.includes('azure_policy_change_monitor');
  _extensionView = view === 'monitor' && monitorEnabled ? 'monitor' : 'plugins';
  const panels = {plugins: 'extension-plugin-workspace', monitor: 'extension-monitor-workspace'};
  for (const [name, id] of Object.entries(panels)) {
    const panel = document.getElementById(id);
    if (panel) panel.hidden = name !== _extensionView;
    document.getElementById(`extension-${name}-btn`)?.setAttribute('aria-pressed', String(name === _extensionView));
  }
}

function pluginAllowsTab(name) {
  const plugin = PLUGIN_TABS[name];
  return !plugin || _pluginSelection.enabled.includes(plugin);
}

function applyPluginVisibility() {
  const monitorEnabled = _pluginSelection.enabled.includes('azure_policy_change_monitor');
  const monitorButton = document.getElementById('extension-monitor-btn');
  if (monitorButton) monitorButton.hidden = !monitorEnabled;
  if (!_pluginDetailId) switchExtensionView(_extensionView);
  for (const action of document.querySelectorAll('[data-monitor-action]')) action.hidden = !monitorEnabled;
  for (const [tab, plugin] of Object.entries(PLUGIN_TABS)) {
    const enabled = _me?.role === 'admin' && _pluginSelection.enabled.includes(plugin);
    const button = document.getElementById(`tab-${tab}-btn`);
    if (button) button.style.display = enabled ? '' : 'none';
    if (tab === 'report') {
      for (const element of document.querySelectorAll('[data-audit-tab="report"], [data-subtab="report"]')) {
        element.hidden = !enabled;
      }
    }
  }
  for (const [type, plugin] of Object.entries({
    browseragent_task: 'browseragent', agent_activation: 'workflows', http_request: 'workflows',
    extension_task: 'azure_policy_change_monitor',
  })) {
    for (const option of document.querySelectorAll(`option[value="${type}"]`)) {
      option.disabled = !_pluginSelection.enabled.includes(plugin);
      option.hidden = option.disabled;
    }
  }
}

async function loadPluginSelection() {
  const catalog = await api('GET', '/admin/plugins');
  _pluginCatalog = catalog.items;
  _pluginSelection = catalog.selection;
  applyPluginVisibility();
  renderPluginCatalog();
  decoratePluginButtons();
}

function renderPluginCatalog() {
  const root = document.getElementById('plugin-catalog');
  if (!root) return;
  populatePluginCategoryFilter();
  root.replaceChildren();
  for (const plugin of _pluginCatalog) {
    const row = document.createElement('div');
    row.className = 'plugin-card';
    row.dataset.pluginCard = plugin.id;
    const header = document.createElement('div');
    header.className = 'plugin-card-head';
    const label = document.createElement('div');
    const category = document.createElement('span');
    category.className = 'plugin-category';
    category.textContent = pluginCategory(plugin);
    const title = document.createElement('h4');
    title.textContent = plugin.name;
    label.append(category, title);
    const badge = document.createElement('span');
    badge.dataset.pluginStatus = plugin.id;
    const badges = document.createElement('div');
    badges.className = 'plugin-card-badges';
    const pricing = plugin.pricing ?? 'free';
    const priceBadge = document.createElement('span');
    priceBadge.className = 'pill plugin-price ' + (pricing === 'free' ? 'plugin-price-free'
      : pricing === 'paid' ? 'plugin-price-paid' : 'plugin-price-unknown');
    priceBadge.textContent = pricing === 'free' ? 'Free' : pricing === 'paid' ? 'Paid' : 'Pricing unavailable';
    priceBadge.setAttribute('aria-label', 'Pricing: ' + priceBadge.textContent);
    badges.append(priceBadge, badge);
    const description = document.createElement('p');
    description.className = 'plugin-description';
    description.textContent = plugin.description + (plugin.available ? '' : ' Not included in this distribution.');
    header.append(label, badges);
    const controls = document.createElement('div');
    controls.className = 'plugin-controls';
    for (const field of ['installed', 'enabled']) {
      const wrapper = document.createElement('label');
      wrapper.className = 'schedule-check';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.dataset.plugin = plugin.id;
      input.dataset.field = field;
      input.setAttribute('aria-label', `${field === 'installed' ? 'Install' : 'Enable'} ${plugin.name}`);
      input.checked = plugin[field];
      input.disabled = !plugin.available;
      input.addEventListener('change', () => {
        const peerField = field === 'installed' ? 'enabled' : 'installed';
        const peer = root.querySelector(`input[data-plugin="${plugin.id}"][data-field="${peerField}"]`);
        if (field === 'installed' && !input.checked) peer.checked = false;
        if (field === 'enabled' && input.checked) peer.checked = true;
        updatePluginSelectionStatus();
      });
      wrapper.append(input, document.createTextNode(field === 'installed' ? 'Installed' : 'Enabled'));
      controls.append(wrapper);
    }
    const actions = document.createElement('div');
    actions.className = 'plugin-card-actions';
    const settings = document.createElement('button');
    settings.className = 'btn btn-sm';
    settings.innerHTML = pluginIcon('settings') + 'Settings';
    settings.disabled = !plugin.installed;
    settings.setAttribute('aria-label', `Settings for ${plugin.name}`);
    settings.setAttribute('aria-controls', 'plugin-detail-workspace');
    settings.addEventListener('click', () => openPluginDetails(plugin.id));
    actions.append(settings);
    const destination = pluginDestination(plugin.id);
    if (destination) {
      const open = document.createElement('button');
      open.className = 'btn btn-sm';
      open.innerHTML = pluginIcon('open') + 'Open';
      open.disabled = !plugin.enabled;
      open.setAttribute('aria-label', `Open ${plugin.name}`);
      open.addEventListener('click', () => {
        if (!_pluginDetailId || !pluginDetailsDirty() || window.confirm('Leave without saving plugin settings?')) switchTab(destination);
      });
      actions.append(open);
    }
    const footer = document.createElement('div');
    footer.className = 'plugin-card-footer';
    footer.append(controls, actions);
    row.append(header, description, footer);
    row.addEventListener('click', event => {
      if (!event.target.closest('button, input, label, a, select')) openPluginDetails(plugin.id);
    });
    root.append(row);
  }
  updatePluginSelectionStatus();
  updatePluginDetailSelection();
}

function updatePluginDetailSelection() {
  const selected = !!_pluginDetailId;
  document.getElementById('plugin-layout').classList.toggle('has-detail', selected);
  document.getElementById('plugin-detail-empty').hidden = selected;
  for (const card of document.getElementById('plugin-catalog').querySelectorAll('[data-plugin-card]')) {
    const active = card.dataset.pluginCard === _pluginDetailId;
    card.classList.toggle('is-selected', active);
    card.querySelector('button[aria-controls="plugin-detail-workspace"]').setAttribute('aria-pressed', String(active));
  }
}

function readPluginSelection() {
  const root = document.getElementById('plugin-catalog');
  const selection = {};
  for (const field of ['installed', 'enabled']) {
    selection[field] = [...root.querySelectorAll(`input[data-field="${field}"]:checked`)].map(input => input.dataset.plugin);
  }
  return selection;
}

function updatePluginSelectionStatus() {
  const root = document.getElementById('plugin-catalog');
  const selection = readPluginSelection();
  const dirty = ['installed', 'enabled'].some(field =>
    [...selection[field]].sort().join(',') !== [..._pluginSelection[field]].sort().join(','));
  for (const plugin of _pluginCatalog) {
    const badge = root.querySelector(`[data-plugin-status="${plugin.id}"]`);
    const enabled = selection.enabled.includes(plugin.id);
    badge.textContent = !plugin.available ? 'Not bundled' : enabled ? 'Enabled'
      : selection.installed.includes(plugin.id) ? 'Disabled' : 'Not installed';
    badge.className = 'pill ' + (enabled ? 'pill-green' : 'pill-gray');
  }
  const count = document.getElementById('plugin-count');
  if (count) count.textContent = `${selection.enabled.length} enabled / ${_pluginCatalog.length} plugins`;
  const status = document.getElementById('plugin-selection-status');
  if (status) status.textContent = dirty ? 'Unsaved changes - save to apply your selection.' : 'No unsaved changes';
  const button = document.getElementById('plugin-save-btn');
  if (button) button.disabled = !dirty;
  applyPluginCatalogView(selection);
}

async function savePluginSelection(button) {
  if (_pluginDetailId && pluginDetailsDirty() && !window.confirm('Apply plugin selection and reload without saving plugin settings?')) return;
  const selection = readPluginSelection();
  button.disabled = true;
  try {
    await api('PUT', '/admin/plugins', selection);
    window.location.reload();
  } catch (error) {
    toast('Plugin selection was not saved: ' + error.message, 'error');
    button.disabled = false;
  }
}

function loadPluginAsset(plugin, forSettings = false) {
  if (!(forSettings && plugin === 'reports' ? _pluginSelection.installed : _pluginSelection.enabled).includes(plugin)) {
    return Promise.reject(new Error(`Plugin '${plugin}' is not installed or is disabled`));
  }
  if (!_pluginAssets.has(plugin)) {
    const loading = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = `/plugins/${plugin}.js`;
      script.onload = resolve;
      script.onerror = () => {
        script.remove();
        _pluginAssets.delete(plugin);
        reject(new Error(`Could not load the ${plugin} plugin`));
      };
      document.head.append(script);
    });
    _pluginAssets.set(plugin, loading);
  }
  return _pluginAssets.get(plugin);
}

function pluginDestination(id) {
  return {automation: 'schedule', browseragent: pluginAllowsTab('schedule') ? 'schedule' : '',
    workflows: pluginAllowsTab('schedule') ? 'schedule' : '', briefing: 'briefing', reports: 'report'}[id] || '';
}

const PLUGIN_SETTINGS_FIELDS = Object.freeze({
  browseragent: [
    ['target_url', 'BrowserAgent command endpoint', 'url'],
    ['timeout_seconds', 'Default timeout (seconds)', 'number', 1, 600],
    ['screenshot', 'Capture screenshot', 'checkbox'], ['console', 'Capture console', 'checkbox'],
    ['metrics', 'Capture metrics', 'checkbox'], ['dom_snapshot', 'Capture DOM snapshot', 'checkbox'],
  ],
  workflows: [
    ['agent_url', 'Agent activation endpoint', 'url'], ['http_url', 'HTTP workflow endpoint', 'url'],
    ['timeout_seconds', 'Default timeout (seconds)', 'number', 1, 600],
    ['payload', 'Default JSON payload (no credentials)', 'json'],
  ],
  notifications: [
    ['enabled', 'Enable shared SMTP transport', 'checkbox'], ['host', 'SMTP host', 'text'],
    ['port', 'SMTP port', 'number', 1, 65535], ['security', 'Security', 'select', ['starttls', 'ssl', 'none']],
    ['username', 'SMTP username', 'text'], ['from_email', 'Sender email', 'email'], ['from_name', 'Sender name', 'text'],
  ],
  briefing: [
    ['project_id', 'Default project ID (empty = all)', 'text'],
    ['refresh_seconds', 'Refresh interval in seconds (0 = manual)', 'number', 0, 3600],
    ['batch_size', 'Initial activities to load', 'number', 1, 25],
  ],
  evaluation: [['trace_enabled', 'Record and evaluate MCP traces', 'checkbox']],
  reports: [
    ['auto_interval', 'Browser auto-generation interval', 'select', ['0', '3600000', '21600000', '86400000', '604800000']],
    ['loop_url', 'Microsoft Loop URL (empty = existing default)', 'url'],
    ['template', 'Markdown report template', 'textarea'],
  ],
});

const PLUGIN_SETTINGS_SECTIONS = Object.freeze({
  browseragent: [
    ['connection', 'Connection', 'settings', ['target_url', 'timeout_seconds']],
    ['capture', 'Capture', 'inspect', ['screenshot', 'console', 'metrics', 'dom_snapshot']],
  ],
  workflows: [
    ['connection', 'Connections', 'settings', ['agent_url', 'http_url', 'timeout_seconds']],
    ['payload', 'Payload', 'edit', ['payload']],
  ],
  notifications: [
    ['transport', 'Transport', 'settings', ['enabled', 'host', 'port', 'security', 'username']],
    ['sender', 'Sender', 'edit', ['from_email', 'from_name']],
  ],
  reports: [
    ['general', 'General', 'settings', ['auto_interval', 'loop_url']],
    ['template', 'Template', 'edit', ['template']],
  ],
});

function renderPluginDetailTabs() {
  const root = document.getElementById('plugin-detail-tabs');
  root.replaceChildren();
  const content = document.getElementById('plugin-detail-content');
  content.removeAttribute('role');
  content.removeAttribute('aria-labelledby');
  const panels = _pluginDetailId === 'azure_policy_change_monitor'
    ? document.getElementById('plugin-detail-monitor-slot').querySelectorAll('[data-plugin-section]')
    : content.querySelectorAll('[data-plugin-section]');
  _pluginDetailSections = [...panels].map(panel => ({
    id: panel.dataset.pluginSection, label: panel.dataset.sectionLabel, icon: panel.dataset.sectionIcon || 'settings', panel,
  }));
  if (!_pluginDetailSections.length) {
    _pluginDetailSections.push({id: 'overview', label: 'Overview', icon: 'inspect'});
  }
  _pluginDetailSections.push({id: 'diagnostics', label: 'Diagnostics', icon: 'inspect',
    panel: document.getElementById('plugin-diagnostics-panel')});
  for (const section of _pluginDetailSections) {
    const tab = document.createElement('button');
    tab.id = `plugin-detail-tab-${section.id}`;
    tab.type = 'button';
    tab.className = 'btn btn-sm';
    tab.setAttribute('role', 'tab');
    tab.innerHTML = pluginIcon(section.icon) + section.label;
    const panel = section.panel || document.getElementById('plugin-detail-content');
    panel.id ||= `plugin-detail-panel-${section.id}`;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', tab.id);
    tab.setAttribute('aria-controls', panel.id);
    tab.addEventListener('click', () => selectPluginDetailTab(section.id));
    tab.addEventListener('keydown', event => {
      const index = _pluginDetailSections.indexOf(section);
      const count = _pluginDetailSections.length;
      const next = event.key === 'ArrowRight' ? (index + 1) % count
        : event.key === 'ArrowLeft' ? (index - 1 + count) % count
        : event.key === 'Home' ? 0 : event.key === 'End' ? count - 1 : -1;
      if (next < 0) return;
      event.preventDefault();
      selectPluginDetailTab(_pluginDetailSections[next].id, true);
    });
    root.append(tab);
  }
  selectPluginDetailTab(_pluginDetailSections[0].id);
}

function selectPluginDetailTab(id, focus = false) {
  const diagnostics = id === 'diagnostics';
  document.getElementById('plugin-detail-content').hidden = diagnostics;
  document.getElementById('plugin-detail-monitor-slot').hidden = diagnostics;
  for (const section of _pluginDetailSections) {
    const active = section.id === id;
    const tab = document.getElementById(`plugin-detail-tab-${section.id}`);
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    if (section.panel) section.panel.hidden = !active;
    if (focus && active) tab.focus();
  }
  if (_pluginDetailId === 'azure_policy_change_monitor') {
    document.querySelector('#extension-monitor-workspace .extension-layout').hidden = diagnostics || id === 'activity';
  }
}

function preparePluginMonitorTabs(monitor) {
  for (const [index, section] of [...monitor.querySelectorAll('.extension-config-block')].entries()) {
    section.dataset.pluginSection = `monitor-${index}`;
    section.dataset.sectionLabel = section.querySelector('summary').textContent;
    section.open = true;
  }
  let activity = document.getElementById('plugin-monitor-activity');
  if (!activity) {
    activity = document.createElement('div');
    activity.id = 'plugin-monitor-activity';
    activity.dataset.pluginSection = 'activity';
    activity.dataset.sectionLabel = 'Activity';
    activity.dataset.sectionIcon = 'schedule';
    activity.append(...monitor.querySelectorAll(':scope > section.briefing-section'));
    monitor.append(activity);
  }
}

function pluginDetailsDirty() {
  if (_pluginDetailId === 'azure_policy_change_monitor') return _pluginMonitorDirty;
  const form = document.getElementById('plugin-settings-form');
  return !!form && JSON.stringify(readPluginSettingsForm(form)) !== _pluginDetailSaved;
}

function updatePluginDetailRoute(id) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set('plugin', id);
  else url.searchParams.delete('plugin');
  window.history.pushState({}, '', url.pathname + url.search + url.hash);
}

function closePluginDetails(updateRoute = true) {
  if (pluginDetailsDirty() && !window.confirm('Discard unsaved plugin settings?')) return false;
  ++_pluginDetailRequest;
  _pluginDetailId = '';
  _pluginMonitorDirty = false;
  _pluginDetailSections = [];
  document.getElementById('plugin-detail-content').replaceChildren();
  document.getElementById('plugin-detail-workspace').hidden = true;
  document.getElementById('extension-monitor-workspace').hidden = true;
  document.getElementById('extension-plugin-workspace').hidden = false;
  updatePluginDetailSelection();
  if (updateRoute) updatePluginDetailRoute('');
  return true;
}

async function syncPluginDetailFromLocation() {
  const id = new URL(window.location.href).searchParams.get('plugin');
  if (id && id !== _pluginDetailId) await openPluginDetails(id, false);
  else if (!id && _pluginDetailId && !closePluginDetails(false)) updatePluginDetailRoute(_pluginDetailId);
}

async function openPluginDetails(id, updateRoute = true) {
  const plugin = _pluginCatalog.find(item => item.id === id);
  if (!plugin || !plugin.installed) {
    toast('Install and save this plugin before opening settings.', 'error');
    return;
  }
  if (pluginDetailsDirty() && !window.confirm('Discard unsaved plugin settings?')) return;
  const request = ++_pluginDetailRequest;
  _pluginDetailId = id;
  _pluginDetailSaved = '';
  const root = document.getElementById('plugin-detail-content');
  root.replaceChildren();
  root.textContent = 'Loading settings...';
  root.hidden = false;
  document.getElementById('plugin-detail-tabs').replaceChildren();
  document.getElementById('plugin-diagnostics-panel').hidden = true;
  document.getElementById('plugin-detail-scope').textContent = '';
  document.getElementById('extension-plugin-workspace').hidden = false;
  document.getElementById('extension-monitor-workspace').hidden = true;
  document.getElementById('plugin-detail-workspace').hidden = false;
  document.getElementById('plugin-detail-title').textContent = plugin.name;
  document.getElementById('plugin-detail-description').textContent = plugin.description;
  const state = document.getElementById('plugin-detail-state');
  state.textContent = plugin.enabled ? 'Enabled' : 'Disabled - settings remain editable';
  state.className = 'pill ' + (plugin.enabled ? 'pill-green' : 'pill-gray');
  document.getElementById('plugin-detail-diagnostics').hidden = true;
  updatePluginDetailSelection();
  if (updateRoute) updatePluginDetailRoute(id);
  try {
    const data = await api('GET', `/admin/plugins/${id}/settings`);
    if (request !== _pluginDetailRequest || !_jwt || !_me) return;
    let values = data.values;
    if (id === 'reports') {
      await loadPluginAsset('reports', true);
      if (request !== _pluginDetailRequest) return;
      values = {...readReportPreferences(), template: getWsrTemplate()};
    }
    root.replaceChildren();
    const scope = id === 'reports'
      ? 'Browser-local preferences and template. Existing report history is retained.'
      : id === 'browseragent' || id === 'workflows'
        ? 'Runtime defaults for new tasks created in Admin. Existing tasks and explicit task values are unchanged. URLs are reached from the CGA server/container, not your browser.'
        : id === 'notifications'
          ? 'Shared SMTP configuration - the same source used by runtime settings. Password: CGA_SMTP_PASSWORD environment variable. Webhook environment references and severity rules remain with the policy monitor.'
          : id === 'automation'
            ? 'The scheduler stays core. Configure cadence, timeout and payload on each task; no duplicate scheduler configuration is stored here.'
            : id === 'evaluation'
              ? 'Runtime preference. TRACE_ENABLED is the operator gate. Starting trace collection after initially disabled startup requires an API restart; benchmarks do not.'
              : id === 'azure_policy_change_monitor'
                ? 'Existing platform monitor configuration, snapshots and history. No configuration is copied or migrated.'
                : 'Runtime defaults applied when opening Dashboard. Existing activity data is unchanged.';
    document.getElementById('plugin-detail-scope').textContent = scope;
    if (PLUGIN_SETTINGS_FIELDS[id]) renderPluginSettingsForm(id, values);
    if (id === 'azure_policy_change_monitor') {
      _selectedExtensionId = id;
      await loadExtensionProject();
      if (request !== _pluginDetailRequest) return;
      const monitor = document.getElementById('extension-monitor-workspace');
      document.getElementById('plugin-detail-monitor-slot').append(monitor);
      monitor.hidden = false;
      monitor.oninput = () => { _pluginMonitorDirty = true; };
      monitor.onchange = () => { _pluginMonitorDirty = true; };
      preparePluginMonitorTabs(monitor);
      for (const button of monitor.querySelectorAll('[data-monitor-action]')) {
        const needsAutomation = button.getAttribute('onclick')?.includes('scheduleSelectedExtension');
        button.disabled = !plugin.enabled || (needsAutomation && !pluginAllowsTab('schedule'));
        if (needsAutomation) button.title = pluginAllowsTab('schedule') ? 'Schedule a policy scan' : 'Enable Automation to manage scheduled scans';
      }
    }
    if (id === 'automation' || id === 'evaluation') {
      const status = document.createElement('p');
      status.className = 'plugin-detail-note';
      status.textContent = id === 'automation'
        ? `Core scheduler: ${data.status.scheduler_running ? 'running' : 'not running'}.`
        : `Trace worker: ${data.status.worker_running ? 'started' : 'not started'}. Trace gate: ${data.status.tracing_allowed ? 'allowed' : 'off'}.`;
      root.append(status);
    }
    if (id === 'evaluation') {
      const tools = document.createElement('p');
      tools.className = 'plugin-detail-note';
      tools.textContent = 'Run benchmarks explicitly through the CGA MCP tools: run_eval, benchmark_token_efficiency and benchmark_context_quality. This configuration check does not run a benchmark.';
      root.append(tools);
    }
    const destination = pluginDestination(id);
    if (destination) {
      const open = document.createElement('button');
      open.className = 'btn btn-sm';
      open.innerHTML = pluginIcon('open') + (id === 'automation' ? 'Manage tasks and run history' : 'Open workspace');
      open.disabled = !plugin.enabled;
      open.addEventListener('click', () => {
        if (!pluginDetailsDirty() || window.confirm('Leave without saving plugin settings?')) switchTab(destination);
      });
      root.append(open);
    }
    if (id === 'browseragent' || id === 'workflows') {
      const note = document.createElement('p');
      note.className = 'plugin-detail-note';
      note.textContent = 'Use Automation for task editing and explicit manual execution. Without Automation, use the authenticated adapter task API; enabling a plugin alone does not verify its external service.';
      root.append(note);
    }
    renderPluginDetailTabs();
    decoratePluginButtons();
  } catch (error) {
    if (request !== _pluginDetailRequest) return;
    root.textContent = 'Settings could not be loaded: ' + error.message;
    toast(root.textContent, 'error');
  }
}

function renderPluginSettingsForm(id, values) {
  const form = document.createElement('form');
  form.id = 'plugin-settings-form';
  form.className = 'plugin-detail-form';
  const sections = PLUGIN_SETTINGS_SECTIONS[id] || [['general', 'General', 'settings', PLUGIN_SETTINGS_FIELDS[id].map(field => field[0])]];
  const panels = new Map();
  for (const [key, title, icon] of sections) {
    const panel = document.createElement('div');
    panel.dataset.pluginSection = key;
    panel.dataset.sectionLabel = title;
    panel.dataset.sectionIcon = icon;
    panels.set(key, panel);
    form.append(panel);
  }
  for (const [key, title, type, min, max] of PLUGIN_SETTINGS_FIELDS[id]) {
    const group = document.createElement('div');
    group.className = 'form-group';
    const label = document.createElement('label');
    label.textContent = title;
    label.htmlFor = `plugin-setting-${key}`;
    const input = document.createElement(type === 'textarea' || type === 'json' ? 'textarea' : type === 'select' ? 'select' : 'input');
    input.id = label.htmlFor;
    input.name = key;
    input.dataset.settingType = type;
    if (type === 'select') {
      const intervalLabels = {'0': 'Off', '3600000': 'Hourly', '21600000': 'Every 6 hours', '86400000': 'Daily', '604800000': 'Weekly'};
      for (const option of min) {
        const element = document.createElement('option');
        element.value = option;
        element.textContent = key === 'auto_interval' ? intervalLabels[option] : option;
        input.append(element);
      }
    } else if (type !== 'textarea' && type !== 'json') input.type = type;
    if (type === 'checkbox') input.checked = !!values[key];
    else input.value = type === 'json' ? JSON.stringify(values[key], null, 2) : String(values[key] ?? '');
    if (type === 'number') { input.min = min; input.max = max; input.step = '1'; }
    if (key === 'template') input.required = true;
    group.append(label, input);
    panels.get(sections.find(section => section[3].includes(key))[0]).append(group);
  }
  const status = document.createElement('p');
  status.id = 'plugin-settings-status';
  status.setAttribute('role', 'status');
  status.textContent = 'No unsaved changes';
  const save = document.createElement('button');
  save.className = 'btn btn-primary btn-sm';
  save.type = 'submit';
  save.innerHTML = pluginIcon('save') + 'Save settings';
  save.disabled = true;
  form.append(status, save);
  form.addEventListener('input', () => {
    save.disabled = !pluginDetailsDirty();
    status.textContent = save.disabled ? 'No unsaved changes' : 'Unsaved settings';
  });
  form.addEventListener('change', () => {
    save.disabled = !pluginDetailsDirty();
    status.textContent = save.disabled ? 'No unsaved changes' : 'Unsaved settings';
  });
  form.addEventListener('submit', event => { event.preventDefault(); savePluginSettings(form, save); });
  form.addEventListener('invalid', event => {
    const panel = event.target.closest('[data-plugin-section]');
    if (panel) selectPluginDetailTab(panel.dataset.pluginSection);
  }, true);
  document.getElementById('plugin-detail-content').append(form);
  _pluginDetailSaved = JSON.stringify(readPluginSettingsForm(form));
}

function readPluginSettingsForm(form) {
  const result = {};
  for (const input of form.querySelectorAll('[data-setting-type]')) {
    const type = input.dataset.settingType;
    result[input.name] = type === 'checkbox' ? input.checked : type === 'number' ? Number(input.value) : input.value;
  }
  return result;
}

function readReportPreferences() {
  const raw = localStorage.getItem('cga_reports_preferences');
  return raw ? JSON.parse(raw) : {auto_interval: '604800000', loop_url: ''};
}

async function savePluginSettings(form, button) {
  const id = _pluginDetailId;
  const values = readPluginSettingsForm(form);
  button.disabled = true;
  try {
    const payload = {...values};
    for (const input of form.querySelectorAll('[data-setting-type="json"]')) payload[input.name] = JSON.parse(input.value);
    if (id === 'reports') {
      if (values.loop_url) {
        const url = new URL(values.loop_url);
        if (url.protocol !== 'https:' || url.username || url.password) throw new Error('Loop URL must use HTTPS without embedded credentials');
      }
      localStorage.setItem('cga_reports_preferences', JSON.stringify({auto_interval: values.auto_interval, loop_url: values.loop_url}));
      localStorage.setItem('cga_wsr_template', values.template);
      const template = document.getElementById('wsr-template');
      if (template) template.value = values.template;
      const auto = document.getElementById('wsr-auto');
      if (auto) {
        auto.value = values.auto_interval;
        if (_pluginSelection.enabled.includes('reports')) updateWsrAutoSchedule();
      }
    } else {
      await api('PUT', `/admin/plugins/${id}/settings`, payload);
      _pluginRuntimeSettings[id] = payload;
    }
    if (id !== _pluginDetailId) return;
    _pluginDetailSaved = JSON.stringify(values);
    const changedDuringSave = JSON.stringify(readPluginSettingsForm(form)) !== _pluginDetailSaved;
    button.disabled = !changedDuringSave;
    document.getElementById('plugin-settings-status').textContent = changedDuringSave
      ? 'Submitted settings saved; newer edits are unsaved' : 'Settings saved';
    toast('Plugin settings saved');
  } catch (error) {
    if (id !== _pluginDetailId) return;
    document.getElementById('plugin-settings-status').textContent = 'Not saved: ' + error.message;
    toast('Plugin settings were not saved: ' + error.message, 'error');
    button.disabled = false;
  }
}

async function inspectPluginDetails(button) {
  const id = _pluginDetailId;
  const output = document.getElementById('plugin-detail-diagnostics');
  button.disabled = true;
  try {
    const data = await api('GET', `/admin/plugins/${id}/settings`);
    if (id !== _pluginDetailId) return;
    const result = {plugin: id, enabled: data.enabled, scope: data.scope, ...data.status};
    if (id === 'notifications') result.smtp_password_configured = data.values.password_set;
    if (id === 'browseragent' || id === 'workflows') {
      result.external_connection = 'Not tested. Manual execution is an explicit task action.';
      result.recent_tasks_up_to_100 = (await api('GET', `/admin/plugins/${id}/tasks?limit=100`)).length;
    }
    if (id === 'reports') result.saved_template = !!localStorage.getItem('cga_wsr_template');
    if (id !== _pluginDetailId) return;
    output.textContent = JSON.stringify(result, null, 2);
    output.hidden = false;
  } catch (error) {
    toast('Configuration check failed: ' + error.message, 'error');
  } finally { button.disabled = false; }
}

async function loadPluginRuntimeDefaults(id) {
  const data = await api('GET', `/admin/plugins/${id}/settings`);
  _pluginRuntimeSettings[id] = data.values;
  return data.values;
}

function disposePluginDetails() {
  ++_pluginDetailRequest;
  _pluginDetailId = '';
  _pluginDetailSaved = '';
  _pluginDetailSections = [];
  _pluginMonitorDirty = false;
  _pluginRuntimeSettings = {};
  _pluginSelection = {installed: [], enabled: []};
  _pluginCatalog = [];
  document.getElementById('plugin-detail-content').replaceChildren();
  document.getElementById('plugin-detail-workspace').hidden = true;
  document.getElementById('extension-monitor-workspace').hidden = true;
  updatePluginDetailSelection();
}

async function loadWorkReport() {
  try {
    if (_jwt && _me) await ReportWorkspace.load();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function loadWorkBriefing(options = {}) {
  try {
    if (options.useDefaults) {
      const defaults = await loadPluginRuntimeDefaults('briefing');
      if (!_jwt || !_me) return;
      document.getElementById('briefing-project-id').value = defaults.project_id;
      document.getElementById('briefing-limit').value = defaults.batch_size;
      if (_briefingPollInterval) clearInterval(_briefingPollInterval);
      _briefingPollInterval = defaults.refresh_seconds > 0 ? setInterval(() => {
        if (document.getElementById('pane-briefing')?.classList.contains('active')) {
          loadWorkBriefing({preserveLimit: true});
        }
      }, defaults.refresh_seconds * 1000) : null;
    }
    await loadPluginAsset('briefing');
    if (_jwt && _me) return await loadBriefingPlugin(options);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function initWsrTemplateEditor() {
  try {
    await loadPluginAsset('reports');
    if (_jwt && _me) initReportsTemplateEditor();
  } catch (error) {
    toast(error.message, 'error');
  }
}
