'use strict';

const ReportWorkspace = (() => {
  let snapshot = null, libraries = null, requestId = 0, pageIndex = 0, exporting = false;
  const charts = new Map();
  const PAGE_SIZE = 25;
  const get = id => document.getElementById(id);
  const text = (tag, content, parent, className = '') => {
    const node = document.createElement(tag);
    node.textContent = content;
    node.className = className;
    if (parent) parent.append(node);
    return node;
  };
  const projectName = id => _projectNameById[id] || id;
  const fmt = count => Number(count).toLocaleString();
  const pickedSections = () => [...document.querySelectorAll('[data-report-section]:checked')].map(e => e.value);
  const productVersion = () => {
    const version = get('app-version').textContent.trim().replace(/^v/, '');
    if (!/^\d+\.\d+\.\d+$/.test(version)) throw new Error('Product version is unavailable; reload before exporting.');
    return version;
  };
  const exportName = extension => `CGA-Report-v${productVersion()}-en.${extension}`;

  function status(message, error = false) {
    get('report-status').textContent = message;
    get('report-status').dataset.error = String(error);
  }

  function buttons() {
    const dirty = snapshot && JSON.stringify(filters()) !== JSON.stringify(snapshot.draft);
    const busy = get('report-workspace').getAttribute('aria-busy') === 'true';
    for (const button of document.querySelectorAll('[data-report-export]')) {
      button.disabled = !snapshot || Boolean(dirty) || exporting || busy;
    }
    const message = 'Filters changed. Generate a new snapshot before exporting.';
    if (dirty) status(message);
    else if (snapshot && get('report-status').textContent === message) {
      status('Filters match the saved snapshot. Exports use that snapshot.');
    }
  }

  function range() {
    if (get('report-period').value === 'custom') return;
    const end = new Date();
    const start = new Date(end.getTime() - (Number(get('report-period').value) - 1) * 86400000);
    get('report-start').value = start.toISOString().slice(0, 10);
    get('report-end').value = end.toISOString().slice(0, 10);
    buttons();
  }

  function filters() {
    return {
      start: get('report-start').value, end: get('report-end').value,
      project_ids: [...get('report-projects').selectedOptions].map(option => option.value),
      event_type: get('report-type').value.trim(), status: get('report-state').value.trim(),
      search: get('report-search').value.trim(), sections: pickedSections(),
    };
  }

  function requestFilters(draft) {
    const start = new Date(`${draft.start}T00:00:00Z`);
    const end = new Date(`${draft.end}T00:00:00Z`);
    end.setUTCDate(end.getUTCDate() + 1);
    if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime())
      || end <= start || end - start > 366 * 86400000) {
      throw new Error('Choose a valid UTC date range of at most 366 days.');
    }
    if (!draft.sections.length) throw new Error('Select at least one report section.');
    const {sections, ...query} = draft;
    return {...query, start: start.toISOString(), end: end.toISOString()};
  }

  async function load() {
    const select = get('report-projects');
    const selected = new Set([...select.selectedOptions].map(option => option.value));
    select.replaceChildren();
    for (const project of _allProjects) {
      const option = text('option', project.project_name, select);
      option.value = project.project_id;
      option.selected = selected.has(option.value);
    }
    if (!get('report-start').value) range();
    if (snapshot) resize();
    else status('Choose your scope and generate a report. Nothing is written to project data.');
    buttons();
  }

  async function generate() {
    const id = ++requestId, session = _jwt;
    let draft, query;
    try { draft = filters(); query = requestFilters(draft); }
    catch (error) { status(error.message, true); return; }
    get('report-generate').disabled = true;
    get('report-workspace').setAttribute('aria-busy', 'true');
    for (const button of document.querySelectorAll('[data-report-export]')) button.disabled = true;
    status('Building a filtered, read-only snapshot...');
    try {
      const needsActivity = draft.sections.some(item => item !== 'readiness');
      const needsReadiness = draft.sections.includes('readiness');
      const readiness = async () => {
        const ids = draft.project_ids.length ? draft.project_ids : [''];
        const responses = [];
        for (const project of ids) responses.push(await api('GET', '/admin/ai-first/readiness'
          + (project ? '?project_id=' + encodeURIComponent(project) : '')));
        return responses.flatMap(item => item.projects);
      };
      const [activity, projects, libs] = await Promise.all([
        needsActivity ? api('POST', '/admin/reports/generate', query) : null,
        needsReadiness ? readiness() : [],
        libraries || (libraries = import('/report-vendor/report-libraries.js')),
      ]);
      if (id !== requestId || session !== _jwt || !_me) return;
      snapshot = {draft, query, activity, projects, generated_at: new Date().toISOString()};
      pageIndex = 0;
      get('report-results').hidden = false;
      render(libs);
      status(`Snapshot generated ${_fmtIso(snapshot.generated_at)}. Filters use UTC; exports use this exact snapshot.`);
    } catch (error) {
      libraries = null;
      if (id !== requestId || session !== _jwt) return;
      status(`Report not generated: ${error.message}. Build local report assets if libraries are unavailable.`, true);
      toast('Report generation failed: ' + error.message, 'error');
    } finally {
      if (id === requestId) {
        get('report-generate').disabled = false;
        get('report-workspace').setAttribute('aria-busy', 'false');
        buttons();
      }
    }
  }

  function chart(id, option, libs) {
    const node = get(id);
    if (charts.has(id)) charts.get(id).dispose();
    const style = getComputedStyle(get('report-workspace'));
    const ink = style.getPropertyValue('--text').trim() || '#d8e3f0';
    const muted = style.getPropertyValue('--muted').trim();
    const border = style.getPropertyValue('--border').trim();
    const instance = libs.echarts.init(node, null, {renderer: 'svg'});
    const axisStyle = {axisLabel: {color: muted}, axisLine: {lineStyle: {color: border}}, splitLine: {lineStyle: {color: border}}};
    instance.setOption({
      color: ['#4fbaaa', '#5999e8', '#dda858', '#b888dc', '#dc7585'],
      backgroundColor: 'transparent', textStyle: {color: ink},
      aria: {enabled: true, decal: {show: true}},
      tooltip: {trigger: 'item', renderMode: 'richText'},
      ...option,
      xAxis: {...axisStyle, ...option.xAxis},
      yAxis: {...axisStyle, ...option.yAxis},
    });
    charts.set(id, instance);
    return instance;
  }

  function bars(id, values, libs, name = key => key, action = null) {
    const entries = Object.entries(values).sort((a, b) => b[1] - a[1]).slice(0, 12).reverse();
    const instance = chart(id, {
      grid: {left: 140, right: 30, top: 20, bottom: 35},
      xAxis: {type: 'value', minInterval: 1, splitNumber: 3}, yAxis: {type: 'category', data: entries.map(([key]) => name(key))},
      series: [{type: 'bar', data: entries.map(([, count]) => count), barMaxWidth: 22}],
    }, libs);
    if (action) instance.on('click', event => { action(entries[event.dataIndex][0]); buttons(); });
  }

  function render(libs) {
    const {activity, projects, draft} = snapshot;
    const counts = activity?.counts;
    const knownScores = projects.map(item => item.overall_score).filter(value => value !== null);
    const kpis = get('report-kpis');
    kpis.replaceChildren();
    for (const [label, value, note] of [
      ['Recorded events', counts ? fmt(counts.events) : 'Not selected', activity?.coverage.truncated ? 'Partial sample' : 'In selected UTC window'],
      ['Active projects', counts ? fmt(counts.projects) : 'Not selected', 'Projects with matching activity'],
      ['Readiness', knownScores.length ? Math.round(knownScores.reduce((a, b) => a + b, 0) / knownScores.length) + ' / 100' : 'Unknown',
        'Current observed evidence; not historical ROI'],
      ['Evidence coverage', `${knownScores.length} / ${projects.length}`, 'Projects with a known readiness score'],
    ]) {
      const card = text('div', '', kpis, 'report-kpi');
      text('span', label, card); text('strong', value, card); text('span', note, card);
    }
    get('report-coverage').textContent = [activity?.coverage.note, activity?.coverage.scope,
      projects.length ? 'Readiness is a current snapshot. Dates, type, status and text filters apply only to activity.' : ''].filter(Boolean).join(' ');
    for (const [section, element] of [['activity', 'report-activity'], ['readiness', 'report-readiness'], ['evidence', 'report-evidence']]) {
      get(element).hidden = !draft.sections.includes(section);
    }
    if (draft.sections.includes('activity')) {
      const dates = [], values = [];
      for (let day = new Date(snapshot.query.start); day < new Date(snapshot.query.end); day.setUTCDate(day.getUTCDate() + 1)) {
        const key = day.toISOString().slice(0, 10);
        dates.push(key); values.push(counts.by_day[key] || 0);
      }
      chart('report-trend', {
        tooltip: {trigger: 'axis', renderMode: 'richText'},
        grid: {left: 50, right: 25, top: 25, bottom: 65},
        xAxis: {type: 'category', data: dates}, yAxis: {type: 'value', minInterval: 1},
        dataZoom: [{type: 'inside'}, {type: 'slider', height: 18}],
        series: [{type: 'line', data: values, smooth: false, areaStyle: {opacity: .12}}],
      }, libs);
      bars('report-project-chart', counts.by_project, libs, projectName, id => {
        for (const option of get('report-projects').options) option.selected = option.value === id;
      });
      bars('report-status-chart', counts.by_status, libs, key => key, key => { get('report-state').value = key; });
      const list = get('report-type-options'); list.replaceChildren();
      for (const key of Object.keys(counts.by_type)) text('option', key, list).value = key;
      const states = get('report-state-options'); states.replaceChildren();
      for (const key of Object.keys(counts.by_status)) text('option', key, states).value = key;
    }
    if (draft.sections.includes('readiness')) {
      const values = {};
      for (const project of projects) for (const dimension of project.dimensions) {
        (values[dimension.label] ||= []).push(dimension.score);
      }
      const averages = Object.fromEntries(Object.entries(values).flatMap(([key, scores]) => {
        const known = scores.filter(value => value !== null);
        return known.length ? [[key, Math.round(known.reduce((a, b) => a + b, 0) / known.length)]] : [];
      }));
      bars('report-readiness-chart', averages, libs);
      const root = get('report-readiness-details'); root.replaceChildren();
      for (const project of projects) {
        const details = text('details', '', root);
        text('summary', `${project.project.project_name}: ${project.overall_score ?? 'Unknown'} / 100 · ${project.overall_status}`, details);
        for (const dimension of project.dimensions) {
          text('h4', `${dimension.label} · ${dimension.score ?? 'Unknown'}`, details);
          for (const signal of dimension.signals) text('p', `${signal.status} · ${signal.label}: ${signal.summary}`, details);
        }
        for (const action of project.recommended_next_actions) text('p', `Next: ${action}`, details);
      }
    }
    evidencePage(0);
  }

  function evidencePage(delta) {
    if (!snapshot) return;
    const rows = snapshot.activity?.activities || [];
    pageIndex = Math.max(0, Math.min(Math.max(0, Math.ceil(rows.length / PAGE_SIZE) - 1), pageIndex + delta));
    const root = get('report-evidence-body'); root.replaceChildren();
    for (const row of rows.slice(pageIndex * PAGE_SIZE, (pageIndex + 1) * PAGE_SIZE)) {
      const tr = text('tr', '', root);
      for (const value of [row.occurred_at.slice(0, 16).replace('T', ' '), projectName(row.project_id), row.event_type, row.status]) text('td', value, tr);
      const td = text('td', '', tr), details = text('details', '', td);
      text('summary', row.title, details); text('p', row.summary || 'No summary recorded.', details);
      text('small', `Activity ID: ${row.activity_id}`, details);
    }
    if (!rows.length) {
      const tr = text('tr', '', root), cell = text('td', 'No matching activity. Absence of records is not proof of no work.', tr);
      cell.colSpan = 5;
    }
    get('report-page-label').textContent = rows.length ? `${pageIndex * PAGE_SIZE + 1}–${Math.min((pageIndex + 1) * PAGE_SIZE, rows.length)} of ${fmt(rows.length)}` : '0 records';
    get('report-prev').disabled = pageIndex === 0;
    get('report-next').disabled = (pageIndex + 1) * PAGE_SIZE >= rows.length;
  }

  function resize() { for (const instance of charts.values()) instance.resize(); }
  async function themeChanged() {
    const data = snapshot;
    if (data && libraries && get('pane-report') && !get('pane-report').hidden) {
      const libs = await libraries;
      if (snapshot === data) render(libs);
    }
  }
  new ResizeObserver(resize).observe(document.documentElement);
  new MutationObserver(themeChanged).observe(document.documentElement, {attributes: true, attributeFilter: ['class', 'data-theme', 'style']});
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', themeChanged);

  function download(content, extension, mime) {
    const filename = exportName(extension);
    const blob = new Blob([content], {type: mime});
    const url = URL.createObjectURL(blob), anchor = document.createElement('a');
    anchor.href = url; anchor.download = filename; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function mermaidSource(data = snapshot) {
    const count = data.activity?.counts;
    const nodes = [`flowchart TB`, `scope["Filtered CGA report"]`,
      `scope --> activity["${count ? count.events + ' recorded events' : 'Activity not selected'}"]`,
      `scope --> readiness["${data.projects.length} current readiness snapshots"]`];
    for (const [index, [name, value]] of Object.entries(count?.by_status || {}).slice(0, 8).entries()) {
      nodes.push(`activity --> status${index}["${mermaidLabel(name)}: ${value} events"]`);
    }
    return nodes.join('\n');
  }

  function mermaidLabel(value) {
    const characters = Array.from(String(value));
    return characters.slice(0, 80).map(character => /^[a-zA-Z0-9 .,_:/()-]$/.test(character)
      ? character : `#${character.codePointAt(0)};`).join('') + (characters.length > 80 ? '...' : '');
  }

  async function exportReport(format) {
    if (!snapshot || exporting) return;
    if (get('report-workspace').getAttribute('aria-busy') === 'true') {
      status('Wait for report generation to finish before exporting.');
      return;
    }
    if (JSON.stringify(filters()) !== JSON.stringify(snapshot.draft)) { buttons(); return; }
    const data = snapshot, session = _jwt;
    exporting = true; buttons();
    try {
      if (format === 'json') download(JSON.stringify(data, null, 2), 'json', 'application/json');
      else if (format === 'mmd') download(mermaidSource(data), 'mmd', 'text/plain');
      else if (format === 'csv') {
        const quote = value => '"' + String(value ?? '').replace(/^(?:\s*[=+@-]|[\t\r\n])/, "'$&").replaceAll('"', '""') + '"';
        const rows = [['UTC time', 'Project', 'Type', 'Status', 'Title', 'Summary', 'Activity ID'],
          ...(data.activity?.activities || []).map(row => [row.occurred_at, projectName(row.project_id), row.event_type, row.status, row.title, row.summary, row.activity_id])];
        download('\uFEFF' + rows.map(row => row.map(quote).join(',')).join('\r\n'), 'csv', 'text/csv;charset=utf-8');
      } else if (format === 'pdf') await pdf(data, session);
    } catch (error) {
      status('Export failed: ' + error.message, true);
      toast('Report export failed: ' + error.message, 'error');
    } finally { exporting = false; buttons(); }
  }

  function checkExportSession(data, session) {
    if (snapshot !== data || session !== _jwt || !_me) {
      throw new Error('Export cancelled because the session or report snapshot changed.');
    }
  }

  async function pdf(data, session) {
    status('Rendering a light, Mermaid-led PDF from the saved snapshot...');
    const {mermaid, html2canvas, jsPDF} = await (await libraries).exportLibraries();
    checkExportSession(data, session);
    mermaid.initialize({startOnLoad: false, securityLevel: 'strict', theme: 'default', flowchart: {htmlLabels: false}});
    const stage = text('div', '', document.body, 'report-export-stage');
    try {
      const pages = [];
      const newPage = title => {
        const page = text('section', '', stage, 'report-export-page'); pages.push(page);
        text('h1', title, page);
        text('p', `${data.draft.start} to ${data.draft.end} UTC · Generated ${data.generated_at}`, page);
        return page;
      };
      const overview = newPage('CGA Report · v' + productVersion());
      text('p', `Projects: ${data.draft.project_ids.map(projectName).join(', ') || 'All projects'}`, overview);
      text('p', `Filters: type=${data.draft.event_type || 'all'}; status=${data.draft.status || 'all'}; text=${data.draft.search || 'none'}`, overview);
      text('p', data.activity?.coverage.note || 'Activity section not selected.', overview);
      text('p', data.activity?.coverage.scope || '', overview);
      const {svg} = await mermaid.render('report-pdf-' + Date.now(), mermaidSource(data));
      const diagram = text('div', '', overview);
      // Strict Mermaid output only; labels are entity-encoded before entering its grammar.
      diagram.innerHTML = svg;
      for (const [name, count] of Object.entries(data.activity?.counts.by_status || {}).slice(0, 8)) {
        text('p', `${name}: ${count}`, overview);
      }
      text('p', 'Readiness is current observed evidence, not a historical trend or proven productivity/ROI.', overview, 'pdf-footnote');
      if (data.draft.sections.includes('activity')) {
        const page = newPage('Activity analysis');
        const types = Object.entries(data.activity.counts.by_type).sort((a, b) => b[1] - a[1]).slice(0, 8);
        text('h2', 'Recorded event types · top 8', page);
        if (types.length) {
          const source = 'pie showData\n' + types.map(([name, count]) => `"${mermaidLabel(name)}" : ${count}`).join('\n');
          const {svg: typeSvg} = await mermaid.render('report-types-' + Date.now(), source);
          const typeChart = text('div', '', page); typeChart.innerHTML = typeSvg;
          for (const [name, count] of types) text('p', `${name}: ${count}`, page);
        } else text('p', 'No matching activity records.', page);
        text('h2', 'Recorded project activity · top 8', page);
        for (const [id, count] of Object.entries(data.activity.counts.by_project).sort((a, b) => b[1] - a[1]).slice(0, 8)) {
          text('p', `${projectName(id)}: ${count}`, page);
        }
      }
      for (const project of data.projects) {
        const page = newPage('AI-First · ' + project.project.project_name);
        const source = ['flowchart LR', 'root["Current readiness"]',
          ...project.dimensions.map((dimension, index) => `root --> d${index}["${mermaidLabel(dimension.label)}: ${dimension.score ?? 'Unknown'} / 100"]`)].join('\n');
        const {svg: chartSvg} = await mermaid.render('readiness-' + pages.length + '-' + Date.now(), source);
        const chartRoot = text('div', '', page); chartRoot.innerHTML = chartSvg;
        for (const [index, dimension] of project.dimensions.entries()) {
          text('p', `${index + 1}. ${dimension.label}: ${dimension.score ?? 'Unknown'} / 100 · ${dimension.status}`, page);
        }
        text('h2', 'Recommended next actions', page);
        for (const action of project.recommended_next_actions.slice(0, 5)) text('p', action, page);
      }
      if (data.draft.sections.includes('evidence')) {
        const rows = data.activity?.activities || [];
        for (let index = 0; index < Math.min(rows.length, 100); index += 4) {
          const page = newPage('Activity evidence');
          for (const row of rows.slice(index, index + 4)) {
            text('h2', row.title.slice(0, 180), page);
            text('p', `${row.occurred_at} · ${projectName(row.project_id)} · ${row.event_type} · ${row.status}`, page);
            text('p', row.summary.slice(0, 260), page);
            text('p', row.activity_id, page, 'pdf-footnote');
          }
        }
        text('p', `PDF includes ${Math.min(rows.length, 100)} of ${rows.length} evidence rows; titles/summaries may be shortened. Download JSON/CSV for complete snapshot data.`, overview);
      }
      await document.fonts.ready;
      checkExportSession(data, session);
      const output = new jsPDF({unit: 'mm', format: 'a4', compress: true});
      for (const [index, page] of pages.entries()) {
        if (index) output.addPage();
        const canvas = await html2canvas(page, {scale: 2, backgroundColor: '#ffffff', logging: false});
        checkExportSession(data, session);
        const height = 190 * canvas.height / canvas.width;
        if (height > 277) {
          let offset = 0;
          const sliceHeight = Math.floor(canvas.width * 277 / 190);
          const top = page.getBoundingClientRect().top;
          const blockEnds = [...page.children].map(child =>
            Math.ceil((child.getBoundingClientRect().bottom - top) * canvas.height / page.offsetHeight));
          while (offset < canvas.height) {
            if (offset) output.addPage();
            const slice = document.createElement('canvas');
            const limit = Math.min(offset + sliceHeight, canvas.height);
            const safeEnd = limit < canvas.height ? blockEnds.filter(end => end > offset && end <= limit).pop() : limit;
            slice.width = canvas.width; slice.height = (safeEnd || limit) - offset;
            slice.getContext('2d').drawImage(canvas, 0, offset, canvas.width, slice.height, 0, 0, canvas.width, slice.height);
            output.addImage(slice, 'PNG', 10, 10, 190, 190 * slice.height / slice.width, undefined, 'FAST');
            offset += sliceHeight;
          }
        } else output.addImage(canvas, 'PNG', 10, 10, 190, height, undefined, 'FAST');
      }
      checkExportSession(data, session);
      output.save(exportName('pdf'));
      status('PDF downloaded. Mermaid diagrams use a print-light palette; JSON/CSV preserve complete data.');
    } finally { stage.remove(); }
  }

  function reset() {
    ++requestId; snapshot = null;
    for (const instance of charts.values()) instance.dispose();
    charts.clear();
    get('report-results').hidden = true;
    get('report-generate').disabled = false;
    get('report-workspace').setAttribute('aria-busy', 'false');
    buttons();
  }
  async function tools(kind, opened) {
    if (!opened) {
      if (kind === 'legacy' && typeof _wsrAutoTimer !== 'undefined') {
        clearInterval(_wsrAutoTimer); _wsrAutoTimer = null;
      }
      return;
    }
    if (kind === 'legacy') {
      try { await loadPluginAsset('reports'); loadReportsPlugin(); }
      catch (error) { toast(error.message, 'error'); }
    } else {
      loadAiFirstReadiness(); loadAiFirstPolicyProfiles(); loadAiFirstSignals(); loadAiFirstEvidenceHistory();
    }
  }
  function theme(value) {
    get('report-workspace').dataset.theme = value;
    themeChanged();
  }
  return {load, generate, buttons, range, evidencePage, exportReport, reset, tools, theme};
})();
