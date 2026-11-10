'use strict';

let adcChartsLoading = null;

function adcBuildNavigation(root, layout, panels) {
  const nav = adcElement('div');
  nav.setAttribute('role', 'tablist');
  nav.setAttribute('aria-label', 'ADC management');
  nav.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;margin:16px 0';
  root.insertBefore(nav, layout);
  const labels = {rules:'Main rules', projects:'Project versions', versions:'Versions'};
  adcState.panels = panels;
  adcState.tabButtons = {};
  adcState.activeTab = 'rules';
  for (const [name, panel] of Object.entries(panels)) {
    const button = adcElement('button', labels[name], nav);
    button.className = 'btn';
    button.type = 'button';
    button.id = `adc-tab-${name}`;
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-controls', `adc-panel-${name}`);
    panel.id = `adc-panel-${name}`;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', button.id);
    adcState.tabButtons[name] = button;
    button.addEventListener('click', () => adcAction(() => adcSelectTab(name)));
    button.addEventListener('keydown', event => {
      const names = Object.keys(panels);
      let index = names.indexOf(name);
      if (event.key === 'ArrowRight') index = (index + 1) % names.length;
      else if (event.key === 'ArrowLeft') index = (index + names.length - 1) % names.length;
      else if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = names.length - 1;
      else return;
      event.preventDefault();
      adcState.tabButtons[names[index]].focus();
      adcState.tabButtons[names[index]].click();
    });
  }
  adcUpdateTabs();
}

function adcUpdateTabs() {
  for (const [name, panel] of Object.entries(adcState.panels)) {
    const selected = name === adcState.activeTab;
    panel.hidden = !selected;
    const button = adcState.tabButtons[name];
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
    button.style.borderColor = selected ? 'var(--accent)' : 'var(--border)';
  }
}

async function adcSelectTab(name) {
  adcState.activeTab = name;
  adcUpdateTabs();
  if (name === 'versions') {
    if (!adcState.versionData || adcState.versionsStale) await adcRefreshVersions();
    else await adcRenderVersionChart();
  }
}

function adcBuildVersionsPanel(parent) {
  adcElement('h3', 'Versions: baseline releases, project revisions and adoption', parent);
  adcElement('p', 'Baseline releases -> immutable project revisions -> projects. Unadopted baselines and unbound projects appear as isolated nodes. Width counts revisions, not code size or compliance. Click a release or revision to inspect it; upgrades and rollback remain explicit project actions.', parent);
  adcButton(parent, 'Refresh versions', adcRefreshVersions);
  const chart = adcElement('div', undefined, parent);
  chart.id = 'adc-version-sankey';
  chart.style.cssText = 'width:100%;height:520px;min-width:0;margin:16px 0';
  chart.setAttribute('role', 'img');
  chart.setAttribute('aria-label', 'ADC baseline to project revision to project Sankey, with nodes aligned right');
  adcState.chartRoot = chart;
  adcState.versionSummary = adcElement('p', '', parent);
  const scroll = adcElement('div', undefined, parent);
  scroll.style.overflowX = 'auto';
  adcState.versionTable = adcElement('table', undefined, scroll);
  adcState.versionTable.style.width = '100%';
}

function adcVersionGraph(data) {
  const nodes = [], links = [];
  const releases = new Map(data.releases.map(r => [r.id, r]));
  const projects = new Map(data.projects.map(p => [p.id, p]));
  const latest = new Map();
  for (const r of data.revisions) latest.set(r.project_id, Math.max(latest.get(r.project_id) || 0, r.revision));
  for (const r of data.releases) nodes.push({name:`release:${r.id}`, display:`ADC ${r.version}`, releaseId:r.id,
    description:`ADC ${r.version}\n${r.actor}\n${r.created_at}\n${r.reason}`});
  for (const p of data.projects) nodes.push({name:`project:${p.id}`, display:p.project_name,
    description:`${p.project_name}\n${latest.has(p.id) ? `Current revision r${latest.get(p.id)}` : 'Unbound: adoption required'}`});
  for (const r of data.revisions) {
    if (!releases.has(r.release_id) || !projects.has(r.project_id)) throw new Error('Version inventory contains an unresolved project or release.');
    const project = projects.get(r.project_id), release = releases.get(r.release_id);
    const current = latest.get(r.project_id) === r.revision;
    const name = `revision:${r.project_id}:${r.revision}`;
    nodes.push({name, display:`${project.project_name} r${r.revision}${current?' (current)':''}`,
      projectId:r.project_id, revision:r.revision,
      description:`${project.project_name} r${r.revision} / ADC ${release.version}\n${r.actor}\n${r.created_at}\n${r.reason}${r.restored_from?`\nRestored from r${r.restored_from}`:''}${r.snapshot_sha256?`\nCollected rules SHA256 ${r.snapshot_sha256}`:''}`});
    const lineStyle = {opacity:current ? 0.6 : 0.2};
    links.push({source:`release:${r.release_id}`, target:name, value:1, lineStyle},
      {source:name, target:`project:${r.project_id}`, value:1, lineStyle});
  }
  return {nodes, links};
}

async function adcLoadCharts() {
  if (window.echarts) return window.echarts;
  if (!adcChartsLoading) {
    adcChartsLoading = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://cdn.jsdelivr.net/npm/echarts@6.0.0/dist/echarts.min.js';
      script.integrity = 'sha384-F07Cpw5v8spSU0H113F33m2NQQ/o6GqPTnTjf45ssG4Q6q58ZwhxBiQtIaqvnSpR';
      script.crossOrigin = 'anonymous';
      script.referrerPolicy = 'no-referrer';
      script.onload = () => {
        if (window.echarts) resolve(window.echarts);
        else { adcChartsLoading = null; script.remove(); reject(new Error('ECharts did not initialize. Version details remain available in the table.')); }
      };
      script.onerror = () => {
        adcChartsLoading = null;
        script.remove();
        reject(new Error('Unable to load the pinned ECharts asset. Check network/CDN access; version details remain available in the table.'));
      };
      document.head.append(script);
    });
  }
  return adcChartsLoading;
}

async function adcRefreshVersions() {
  adcState.versionSummary.textContent = 'Loading version inventory...';
  adcState.chart?.clear();
  adcState.versionData = null;
  adcState.versionTable.replaceChildren();
  try {
    const data = await api('GET', '/adc/versions');
    adcState.versionData = data;
    adcState.versionsStale = false;
    const bound = new Set(data.revisions.map(r => r.project_id));
    adcState.versionSummary.textContent = `${data.releases.length} baseline releases, ${data.revisions.length} project revisions, ${data.projects.length} projects; ${data.projects.length-bound.size} unbound. Historical revisions are included.`;
    const table = adcState.versionTable;
    const head = adcElement('tr', undefined, adcElement('thead', undefined, table));
    for (const title of ['Project', 'Revision', 'ADC baseline', 'Actor / reason', 'Collected rules']) adcElement('th', title, head);
    const body = adcElement('tbody', undefined, table);
    const releases = new Map(data.releases.map(r => [r.id, r.version]));
    for (const p of data.projects) {
      const revisions = data.revisions.filter(r => r.project_id === p.id);
      for (const r of revisions.length ? revisions : [null]) {
        const row = adcElement('tr', undefined, body);
        adcElement('td', p.project_name, row);
        const revisionCell = adcElement('td', r ? '' : 'Unbound', row);
        if (r) adcButton(revisionCell, `r${r.revision}`, () => adcInspectVersion(p.id, r.revision));
        adcElement('td', r ? releases.get(r.release_id) : 'Adoption required', row);
        adcElement('td', r ? `${r.actor}: ${r.reason}` : '', row);
        adcElement('td', r?.snapshot_sha256 || 'No collection receipt on this revision', row);
      }
    }
    if (adcState.activeTab === 'versions') await adcRenderVersionChart();
  } catch (error) {
    adcState.versionSummary.textContent += ` Unable to complete refresh: ${error.message}`;
    throw error;
  }
}

async function adcRenderVersionChart() {
  const state = adcState;
  const charts = await adcLoadCharts();
  if (adcState !== state || state.activeTab !== 'versions' || !state.versionData) return;
  const graph = adcVersionGraph(state.versionData);
  if (!state.chart) {
    state.chart = charts.init(state.chartRoot, null, {renderer:'svg'});
    state.chart.on('click', event => {
      if (event.dataType === 'node' && event.data.projectId) {
        adcAction(() => adcInspectVersion(event.data.projectId, event.data.revision));
      } else if (event.dataType === 'node' && event.data.releaseId) {
        adcAction(() => adcInspectRelease(event.data.releaseId));
      }
    });
    state.chartResize = new ResizeObserver(() => state.chart?.resize());
    state.chartResize.observe(state.chartRoot);
    state.themeObserver = new MutationObserver(() => {
      if (adcState === state && state.activeTab === 'versions') {
        adcAction(adcRenderVersionChart);
      }
    });
    state.themeObserver.observe(document.documentElement, {attributes:true, attributeFilter:['class','style','data-theme']});
    state.themeObserver.observe(document.body, {attributes:true, attributeFilter:['class','style','data-theme']});
  }
  const style = getComputedStyle(document.documentElement);
  const text = style.getPropertyValue('--text').trim() || '#e6ecf5';
  const panel = style.getPropertyValue('--panel').trim() || '#0d1118';
  state.chartRoot.style.height = `${Math.max(520, graph.nodes.length * 22)}px`;
  state.chart.resize();
  state.chart.setOption({
    backgroundColor:'transparent',
    textStyle:{color:text},
    tooltip:{trigger:'item', renderMode:'richText', backgroundColor:panel, textStyle:{color:text},
      formatter:entry => entry.dataType === 'node' ? entry.data.description : '1 project revision'},
    series:[{type:'sankey', nodeAlign:'right', orient:'horizontal', left:12, right:210,
      top:20, bottom:20, nodeGap:12, draggable:false, emphasis:{focus:'adjacency'},
      data:graph.nodes, links:graph.links, lineStyle:{color:'gradient', curveness:0.5},
      label:{color:text, formatter:entry => entry.data.display},
      levels:[{depth:0,itemStyle:{color:'#60a5fa'}},{depth:1,itemStyle:{color:'#a78bfa'}},
        {depth:2,itemStyle:{color:'#4ade80'}}]}],
  }, true);
}

async function adcInspectVersion(projectId, revision) {
  if (adcState.projectDirty && !confirm('Discard staged project changes to inspect this version?')) return;
  adcState.projectSelect.value = String(projectId);
  await adcLoadProject();
  const historic = await api('GET', `/adc/projects/${projectId}?revision=${revision}`);
  adcState.diffOutput.textContent = JSON.stringify(historic, null, 2);
  await adcSelectTab('projects');
}

async function adcInspectRelease(releaseId) {
  if (adcState.dirty && !confirm('Discard the unpublished rule draft to inspect this release?')) return;
  adcState.releaseSelect.value = String(releaseId);
  await adcLoadRelease(releaseId);
  await adcSelectTab('rules');
}

function disposeAdcManager() {
  adcState?.chartResize?.disconnect();
  adcState?.themeObserver?.disconnect();
  adcState?.chart?.dispose();
  adcState = null;
}
