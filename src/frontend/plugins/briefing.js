function refreshWorkBriefing() {
  loadWorkBriefing({ preserveLimit: true });
}

function loadMoreWorkBriefing() {
  if (_workBriefingLoading) return;
  _workBriefingRequestedLimit = Math.min(5000, _workBriefingRequestedLimit + WORK_BRIEFING_BATCH_SIZE);
  loadWorkBriefing({ append: true });
}

function renderBriefingChips(counts, emptyLabel) {
  const entries = Object.entries(counts || {});
  if (!entries.length) {
    return `<span class="briefing-subtle">${esc(emptyLabel)}</span>`;
  }
  return entries
    .sort((left, right) => Number(right[1] || 0) - Number(left[1] || 0) || String(left[0]).localeCompare(String(right[0])))
    .map(([label, value]) => `
      <span class="briefing-chip">
        <span>${esc(label || 'unknown')}</span>
        <strong>${esc(String(value || 0))}</strong>
      </span>
    `)
    .join('');
}

function renderBriefingTags(tags) {
  if (!Array.isArray(tags) || !tags.length) return '';
  return `
    <div class="briefing-tag-list">
      ${tags.map(tag => `<span class="briefing-tag">${esc(String(tag || ''))}</span>`).join('')}
    </div>
  `;
}

const BRIEFING_EVENT_ICONS = {
  milestone: '🎯',
  risk: '⚠️',
  status_update: '📌',
  workflow_complete: '✅',
  documentation: '📚',
  documentation_update: '📝',
  decision: '🧩',
  bug: '🐛',
  release: '🚀',
  test: '🧪',
  deployment: '📦',
};

function renderBriefingEventIcon(eventType) {
  const key = String(eventType || '').toLowerCase();
  const icon = BRIEFING_EVENT_ICONS[key] || '🔹';
  const label = key || 'event';
  return `<span class="briefing-event-icon" title="${esc(label)}" aria-label="${esc(label)}">${icon}</span>`;
}

function _briefingStatusClass(key) {
  if (key === 'done' || key === 'completed' || key === 'closed') return 'is-done';
  if (key === 'in_progress' || key === 'in-progress' || key === 'active') return 'is-progress';
  if (key === 'open' || key === 'new' || key === 'pending') return 'is-open';
  if (key === 'blocked' || key === 'failed' || key === 'error') return 'is-blocked';
  return '';
}

function _normalizeBriefingStatuses(status) {
  const raw = String(status || '').trim();
  if (!raw) return [];
  const parts = raw.split(/[|,;/]+/).map(p => p.trim()).filter(Boolean);
  const seen = new Set();
  return parts.filter(p => {
    const key = p.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function renderBriefingStatusDot(status) {
  const key = String(status || '').toLowerCase();
  const cls = _briefingStatusClass(key);
  const label = key || 'no status';
  return `<span class="briefing-status-dot ${cls}" title="${esc(label)}" aria-label="status ${esc(label)}"></span>`;
}

function renderBriefingStatusText(status) {
  const labels = _normalizeBriefingStatuses(status);
  if (!labels.length) {
    return `<span class="briefing-status-text" title="no status" aria-label="status no status">No status</span>`;
  }
  return labels.map(label => {
    const key = label.toLowerCase();
    const pretty = label.replaceAll('_', ' ');
    const cls = _briefingStatusClass(key);
    return `<span class="briefing-status-text ${cls}" title="${esc(pretty)}" aria-label="status ${esc(pretty)}">${esc(pretty)}</span>`;
  }).join('');
}

function getWorkBriefingActivityCriteria() {
  return {
    projectId: document.getElementById('briefing-filter-project')?.value || '',
    eventType: document.getElementById('briefing-filter-type')?.value || '',
    status: document.getElementById('briefing-filter-status')?.value || '',
    query: (document.getElementById('briefing-filter-search')?.value || '').trim().toLowerCase(),
    range: document.getElementById('briefing-filter-range')?.value || 'all',
    sort: document.getElementById('briefing-sort-order')?.value || WORK_BRIEFING_DEFAULT_SORT,
  };
}

function onWorkBriefingActivityFiltersChanged() {
  renderWorkBriefing(_workBriefingSummary, { preserveViewport: true });
}

function clearWorkBriefingActivityFilters() {
  const projectSelect = document.getElementById('briefing-filter-project');
  const typeSelect = document.getElementById('briefing-filter-type');
  const statusSelect = document.getElementById('briefing-filter-status');
  const searchInput = document.getElementById('briefing-filter-search');
  const rangeSelect = document.getElementById('briefing-filter-range');
  const sortSelect = document.getElementById('briefing-sort-order');
  if (projectSelect) projectSelect.value = '';
  if (typeSelect) typeSelect.value = '';
  if (statusSelect) statusSelect.value = '';
  if (searchInput) searchInput.value = '';
  if (rangeSelect) rangeSelect.value = 'all';
  if (sortSelect) sortSelect.value = WORK_BRIEFING_DEFAULT_SORT;
  renderWorkBriefing(_workBriefingSummary, { preserveViewport: true });
}

function _workBriefingCountMapValue(map, value) {
  const key = String(value || '').trim();
  if (!key) return;
  map.set(key, (map.get(key) || 0) + 1);
}

function _workBriefingHumanizeFilterLabel(value) {
  return String(value || '')
    .replaceAll('_', ' ')
    .replaceAll('-', ' ')
    .replace(/\s+/g, ' ')
    .trim() || 'Unknown';
}

function _workBriefingProjectFilterLabel(projectId) {
  const id = String(projectId || '').trim();
  const name = _projectNameById[id] || '';
  return name ? `${id} - ${name}` : id;
}

function _workBriefingSortedCountEntries(counts) {
  return [...counts.entries()]
    .sort((left, right) => Number(right[1] || 0) - Number(left[1] || 0) || WORK_BRIEFING_COLLATOR.compare(String(left[0]), String(right[0])));
}

function _workBriefingRenderSelectOptions(selectId, allLabel, entries, selectedValue, labelFn) {
  const select = document.getElementById(selectId);
  if (!select) return;
  const selected = String(selectedValue || '');
  const items = entries.map(([value, count]) => [String(value), Number(count || 0)]);
  if (selected && !items.some(([value]) => value === selected)) {
    items.unshift([selected, 0]);
  }
  select.innerHTML = `<option value="">${esc(allLabel)}</option>` + items.map(([value, count]) => {
    const baseLabel = labelFn ? labelFn(value) : _workBriefingHumanizeFilterLabel(value);
    const countLabel = count ? ` (${count})` : '';
    return `<option value="${esc(value)}">${esc(baseLabel + countLabel)}</option>`;
  }).join('');
  select.value = selected;
}

function _workBriefingActivityTimestamp(activity) {
  const parsed = Date.parse(activity?.occurred_at || activity?.synced_at || '');
  return Number.isFinite(parsed) ? parsed : 0;
}

function _workBriefingActivityStatusKeys(activity) {
  const statuses = _normalizeBriefingStatuses(activity?.status).map(status => status.toLowerCase());
  return statuses.length ? statuses : ['no status'];
}

function _workBriefingActivityMatches(activity, criteria, nowMs) {
  if (criteria.projectId && String(activity?.project_id || '') !== criteria.projectId) return false;
  if (criteria.eventType && String(activity?.event_type || '') !== criteria.eventType) return false;
  if (criteria.status && !_workBriefingActivityStatusKeys(activity).includes(criteria.status)) return false;

  const rangeMs = WORK_BRIEFING_DATE_FILTER_MS[criteria.range];
  if (rangeMs) {
    const occurredMs = _workBriefingActivityTimestamp(activity);
    if (!occurredMs || occurredMs < nowMs - rangeMs) return false;
  }

  if (criteria.query) {
    const haystack = [
      activity?.project_id,
      _projectNameById[String(activity?.project_id || '')],
      activity?.workspace_name,
      activity?.event_type,
      activity?.title,
      activity?.summary,
      activity?.body_text,
      activity?.status,
      activity?.priority,
      activity?.owner,
      ...(Array.isArray(activity?.tags) ? activity.tags : []),
    ].filter(Boolean).join(' ').toLowerCase();
    if (!haystack.includes(criteria.query)) return false;
  }

  return true;
}

function _workBriefingCompareText(left, right) {
  return WORK_BRIEFING_COLLATOR.compare(String(left || ''), String(right || ''));
}

function getFilteredWorkBriefingActivities(activities, criteria) {
  const nowMs = Date.now();
  const filtered = activities.filter(activity => _workBriefingActivityMatches(activity, criteria, nowMs));
  filtered.sort((left, right) => {
    const leftTime = _workBriefingActivityTimestamp(left);
    const rightTime = _workBriefingActivityTimestamp(right);
    if (criteria.sort === 'occurred_asc') return leftTime - rightTime;
    if (criteria.sort === 'project_asc') return _workBriefingCompareText(left?.project_id, right?.project_id) || rightTime - leftTime;
    if (criteria.sort === 'type_asc') return _workBriefingCompareText(left?.event_type, right?.event_type) || rightTime - leftTime;
    if (criteria.sort === 'status_asc') return _workBriefingCompareText(left?.status, right?.status) || rightTime - leftTime;
    if (criteria.sort === 'title_asc') return _workBriefingCompareText(left?.title, right?.title) || rightTime - leftTime;
    return rightTime - leftTime;
  });
  return filtered;
}

function renderWorkBriefingActivityControls(activities, visibleCount, totalAvailable, criteria) {
  const projectCounts = new Map();
  const typeCounts = new Map();
  const statusCounts = new Map();
  activities.forEach(activity => {
    _workBriefingCountMapValue(projectCounts, activity?.project_id);
    _workBriefingCountMapValue(typeCounts, activity?.event_type);
    const statuses = _normalizeBriefingStatuses(activity?.status);
    if (statuses.length) {
      statuses.forEach(status => _workBriefingCountMapValue(statusCounts, status.toLowerCase()));
    } else {
      _workBriefingCountMapValue(statusCounts, 'no status');
    }
  });

  _workBriefingRenderSelectOptions('briefing-filter-project', 'All loaded projects', _workBriefingSortedCountEntries(projectCounts), criteria.projectId, _workBriefingProjectFilterLabel);
  _workBriefingRenderSelectOptions('briefing-filter-type', 'All event types', _workBriefingSortedCountEntries(typeCounts), criteria.eventType, _workBriefingHumanizeFilterLabel);
  _workBriefingRenderSelectOptions('briefing-filter-status', 'All statuses', _workBriefingSortedCountEntries(statusCounts), criteria.status, _workBriefingHumanizeFilterLabel);

  const rangeSelect = document.getElementById('briefing-filter-range');
  if (rangeSelect) rangeSelect.value = criteria.range || 'all';
  const sortSelect = document.getElementById('briefing-sort-order');
  if (sortSelect) sortSelect.value = criteria.sort || WORK_BRIEFING_DEFAULT_SORT;

  const filterActive = !!(criteria.projectId || criteria.eventType || criteria.status || criteria.query || (criteria.range && criteria.range !== 'all'));
  const active = !!(filterActive || (criteria.sort && criteria.sort !== WORK_BRIEFING_DEFAULT_SORT));
  const clearBtn = document.getElementById('briefing-clear-filters-btn');
  if (clearBtn) clearBtn.disabled = !active;
  const summaryEl = document.getElementById('briefing-activity-filter-summary');
  if (summaryEl) {
    const loadedCount = activities.length;
    const totalLabel = totalAvailable > loadedCount ? `, ${totalAvailable} total available` : '';
    summaryEl.textContent = filterActive
      ? `Showing ${visibleCount} of ${loadedCount} loaded${totalLabel}`
      : `Showing ${loadedCount} loaded${totalLabel}`;
  }
}

function getWorkBriefingTemplateProjectId(payload) {
  const raw = document.getElementById('briefing-project-id')?.value.trim();
  const explicit = raw ? (_projectIdByName[raw] || raw) : '';
  if (explicit) return explicit;
  if (payload?.project_id) return String(payload.project_id);
  const projectCounts = Object.entries(payload?.project_counts || {})
    .sort((left, right) => Number(right[1] || 0) - Number(left[1] || 0) || String(left[0]).localeCompare(String(right[0])));
  return projectCounts[0]?.[0] || 'PROJECT_ID';
}

function buildWorkBriefingJsonTemplate(payload) {
  const projectId = getWorkBriefingTemplateProjectId(payload);
  return {
    project_id: projectId,
    workspace_name: payload?.activities?.find(activity => activity?.project_id === projectId)?.workspace_name || projectId,
    event_type: 'workflow_complete',
    external_id: `sample:${projectId.toLowerCase()}:workflow-complete`,
    title: 'Completed workflow validation',
    summary: 'Published the latest workflow completion state into CGA.',
    status: 'completed',
    tags: ['sample', 'cga', 'briefing'],
    metadata: {
      workflowName: 'wf-sample',
      workflowRunId: '20260526-run1',
      sourceRepo: projectId,
    },
  };
}

function buildWorkBriefingPowerShellTemplate(payload) {
  const projectId = getWorkBriefingTemplateProjectId(payload);
  const endpoint = `${window.location.origin}/api/project/work-briefing/activity`;
  const jsonPayload = JSON.stringify(buildWorkBriefingJsonTemplate(payload), null, 2);
  return [
    `$headers = @{`,
    `  Authorization = \"Bearer $env:CONTEXTGRAPH_MCP_TOKEN\"`,
    `  \"X-Project-ID\" = \"${projectId}\"`,
    `}`,
    `$body = @'`,
    jsonPayload,
    `'@`,
    `Invoke-RestMethod -Uri \"${endpoint}\" -Method Post -Headers $headers -ContentType \"application/json\" -Body $body`,
  ].join('\n');
}

function buildWorkBriefingPythonTemplate(payload) {
  const projectId = getWorkBriefingTemplateProjectId(payload);
  const endpoint = `${window.location.origin}/api/project/work-briefing/activity`;
  const jsonPayload = JSON.stringify(buildWorkBriefingJsonTemplate(payload), null, 2);
  return [
    `import json`,
    `from urllib import request`,
    ``,
    `payload = ${jsonPayload}`,
    `req = request.Request(`,
    `    \"${endpoint}\",`,
    `    data=json.dumps(payload).encode(\"utf-8\"),`,
    `    method=\"POST\",`,
    `    headers={`,
    `        \"Authorization\": \"Bearer <CONTEXTGRAPH_MCP_TOKEN>\",`,
    `        \"X-Project-ID\": \"${projectId}\",`,
    `        \"Content-Type\": \"application/json\",`,
    `    },`,
    `)`,
    `with request.urlopen(req, timeout=10) as response:`,
    `    print(response.read().decode(\"utf-8\"))`,
  ].join('\n');
}

function renderWorkBriefingQuickStart() {
  const host = document.getElementById('briefing-quick-start');
  if (!host) return;
  host.setAttribute('data-briefing-section', 'quick-start');
  host.classList.add('briefing-collapsible');

  const steps = [
    {
      label: 'STEP 1',
      title: 'Add Project',
      body: 'Open the Project tab, create the project, and set the repository path CGA should track.',
    },
    {
      label: 'STEP 2',
      title: "Create Project's MCP Server Token",
      body: 'Expand the project row, create an mcp token, and copy the token value before closing the dialog.',
    },
    {
      label: 'STEP 3',
      title: 'Full Index',
      body: 'Run Full Index for the project so CGA has the complete repository graph before agent use.',
    },
    {
      label: 'STEP 4',
      title: 'Add using CGA into Agent.md',
      body: 'Add the CGA usage note to Agent.md so the agent queries this project context during work.',
    },
  ];

  host.innerHTML = `
    <div class="briefing-collapsible-head">
      <div>
        <span class="briefing-kicker">Quick Start</span>
        <h3>Prepare a repository for CGA-assisted agents</h3>
      </div>
      ${renderBriefingCollapseButton('quick-start', 'Quick Start')}
    </div>
    <div id="${esc(briefingSectionBodyId('quick-start'))}" data-briefing-section-content>
      <p>Follow these steps once per repository before asking an agent to rely on CGA context.</p>
      <ol class="briefing-quick-start-grid">
        ${steps.map(step => `
          <li class="briefing-quick-start-step">
            <span>${esc(step.label)}</span>
            <strong>${esc(step.title)}</strong>
            <p>${esc(step.body)}</p>
          </li>
        `).join('')}
      </ol>
    </div>
  `;
  applyBriefingSectionCollapse('quick-start');
}

function renderWorkBriefingRequestTemplates(payload) {
  const host = document.getElementById('briefing-request-templates');
  if (!host) return;
  host.setAttribute('data-briefing-section', 'integration-templates');
  host.classList.add('briefing-collapsible');

  const projectId = getWorkBriefingTemplateProjectId(payload);
  const jsonTemplate = JSON.stringify(buildWorkBriefingJsonTemplate(payload), null, 2);
  const powerShellTemplate = buildWorkBriefingPowerShellTemplate(payload);
  const pythonTemplate = buildWorkBriefingPythonTemplate(payload);
  const copyTemplates = {
    powershell: { value: powerShellTemplate, label: 'PowerShell template' },
    python: { value: pythonTemplate, label: 'Python template' },
    json: { value: jsonTemplate, label: 'JSON payload' },
  };

  host.innerHTML = `
    <div class="briefing-collapsible-head">
      <div>
        <span class="briefing-kicker">Integration Templates</span>
        <h3>Post activity into CGA from other repos</h3>
      </div>
      ${renderBriefingCollapseButton('integration-templates', 'Integration Templates')}
    </div>
    <div id="${esc(briefingSectionBodyId('integration-templates'))}" data-briefing-section-content>
      <p>Use any active project-scoped token for <strong>${esc(projectId)}</strong>. These snippets target the live CGA endpoint and can be copied directly into repo helpers, scripts, or local validation steps.</p>
      <div class="briefing-command-pill-row">
        <span class="briefing-command-pill"><span>Endpoint</span><strong>/api/project/work-briefing/activity</strong></span>
        <span class="briefing-command-pill"><span>Project</span><strong>${esc(projectId)}</strong></span>
        <span class="briefing-command-pill"><span>Token</span><strong>mcp</strong></span>
      </div>
      <div class="briefing-template-grid">
        <section class="briefing-template-block">
          <div class="briefing-template-head">
            <div>
              <strong>PowerShell</strong>
              <span>BrowserAgent / ADC scripts</span>
            </div>
            <button class="btn btn-sm btn-ghost" data-copy-template="powershell">Copy</button>
          </div>
          <pre class="briefing-template-code">${esc(powerShellTemplate)}</pre>
        </section>
        <section class="briefing-template-block">
          <div class="briefing-template-head">
            <div>
              <strong>Python</strong>
              <span>WorkAssist / services</span>
            </div>
            <button class="btn btn-sm btn-ghost" data-copy-template="python">Copy</button>
          </div>
          <pre class="briefing-template-code">${esc(pythonTemplate)}</pre>
        </section>
        <section class="briefing-template-block">
          <div class="briefing-template-head">
            <div>
              <strong>Payload JSON</strong>
              <span>Minimal event contract</span>
            </div>
            <button class="btn btn-sm btn-ghost" data-copy-template="json">Copy</button>
          </div>
          <pre class="briefing-template-code">${esc(jsonTemplate)}</pre>
        </section>
      </div>
    </div>
  `;

  host.querySelectorAll('[data-copy-template]').forEach(button => {
    button.addEventListener('click', () => {
      const key = button.getAttribute('data-copy-template');
      const template = key ? copyTemplates[key] : null;
      if (!template) return;
      copyText(template.value, template.label);
    });
  });
  applyBriefingSectionCollapse('integration-templates');
}

function renderWorkBriefing(summary = _workBriefingSummary) {
  const preserveViewport = arguments.length > 1 && !!arguments[1]?.preserveViewport;
  const payload = summary || {
    project_id: null,
    total_events: 0,
    total_available: 0,
    has_more: false,
    duplicate_signal_count: 0,
    suspected_duplicates: [],
    event_type_counts: {},
    status_counts: {},
    project_counts: {},
    activities: [],
  };

  const activities = Array.isArray(payload.activities) ? payload.activities : [];
  const activityCriteria = getWorkBriefingActivityCriteria();
  const filteredActivities = getFilteredWorkBriefingActivities(activities, activityCriteria);
  const projectCounts = payload.project_counts || {};
  const eventEntries = Object.entries(payload.event_type_counts || {})
    .sort((left, right) => Number(right[1] || 0) - Number(left[1] || 0) || String(left[0]).localeCompare(String(right[0])));
  const topProjects = Object.entries(projectCounts)
    .sort((left, right) => Number(right[1] || 0) - Number(left[1] || 0) || String(left[0]).localeCompare(String(right[0])))
    .slice(0, 3);
  const commandCenter = document.getElementById('briefing-command-center');
  const workloadShape = document.getElementById('briefing-workload-shape');
  const tableWrap = document.getElementById('briefing-section-activity-log-body');
  const duplicateWrap = document.getElementById('briefing-section-duplicate-signals-body');
  const currentSignature = JSON.stringify({
    total_events: payload.total_events || 0,
    total_available: payload.total_available || 0,
    duplicate_signal_count: payload.duplicate_signal_count || 0,
    activity_criteria: activityCriteria,
    activities: activities.map(activity => [activity.activity_id, activity.project_id, activity.event_type, activity.title, activity.summary, activity.occurred_at, activity.synced_at, activity.status, activity.suspected_duplicate ? 1 : 0]),
    suspected_duplicates: (payload.suspected_duplicates || []).map(group => [group.group_key, group.duplicate_count]),
  });
  if (preserveViewport && currentSignature === _workBriefingRenderSignature) {
    const loadMoreBtn = document.getElementById('briefing-load-more-btn');
    if (loadMoreBtn) loadMoreBtn.disabled = false;
    return;
  }
  const scrollY = preserveViewport ? window.scrollY : null;
  const tableScrollTop = preserveViewport && tableWrap ? tableWrap.scrollTop : null;
  const duplicateScrollTop = preserveViewport && duplicateWrap ? duplicateWrap.scrollTop : null;

  if (commandCenter) {
    commandCenter.setAttribute('data-briefing-section', 'command-center');
    commandCenter.classList.add('briefing-collapsible');
    const topActivities = activities.slice(0, 3);
    commandCenter.innerHTML = `
      <div class="briefing-collapsible-head">
        <div>
          <span class="briefing-kicker">Command Center</span>
          <h3>${payload.project_id ? `Focus on <span class="project-id-inline">${esc(payload.project_id)}${renderCopyIdButton(payload.project_id, 'copy-id-btn-sm')}</span>` : 'Cross-project work pulse'}</h3>
        </div>
        ${renderBriefingCollapseButton('command-center', 'Command Center')}
      </div>
      <div id="${esc(briefingSectionBodyId('command-center'))}" data-briefing-section-content>
        <p>${payload.total_events
          ? `Tracking ${esc(String(payload.total_events))} recent event${Number(payload.total_events) === 1 ? '' : 's'} across the current dashboard scope.`
          : 'No work events have been captured yet. Once MCP activity arrives, this panel will summarize what deserves attention first.'}</p>
        <div class="briefing-command-pill-row">
          <span class="briefing-command-pill"><span>Scope</span><strong>${payload.project_id ? `<span class="project-id-inline">${esc(payload.project_id)}${renderCopyIdButton(payload.project_id, 'copy-id-btn-sm')}</span>` : 'All projects'}</strong></span>
          <span class="briefing-command-pill"><span>Projects</span><strong>${esc(String(Object.keys(projectCounts).length))}</strong></span>
          <span class="briefing-command-pill"><span>Latest</span><strong>${esc(activities[0]?.occurred_at ? relativeTimeLabel(activities[0].occurred_at) : 'none')}</strong></span>
        </div>
        <div class="briefing-focus-list">
          ${topActivities.length
            ? topActivities.map(activity => `
              <article class="briefing-focus-item">
                <strong>${esc(activity.title || activity.event_type || 'Activity')}</strong>
                <span>${activity.project_id ? `<span class="project-id-inline">${esc(activity.project_id)}${renderCopyIdButton(activity.project_id, 'copy-id-btn-sm')}</span>` : 'unknown project'} · ${esc(activity.summary || activity.body_text || 'No summary provided')}</span>
              </article>
            `).join('')
            : `<div class="briefing-empty">Recent focus items will appear here after activity is recorded.</div>`}
        </div>
      </div>
    `;
  }

  if (workloadShape) {
    workloadShape.setAttribute('data-briefing-section', 'workload-shape');
    workloadShape.classList.add('briefing-collapsible');
    workloadShape.innerHTML = `
      <div class="briefing-collapsible-head">
        <div>
          <span class="briefing-kicker">Workload Shape</span>
          <h3>Activity distribution</h3>
        </div>
        ${renderBriefingCollapseButton('workload-shape', 'Workload Shape')}
      </div>
      <div id="${esc(briefingSectionBodyId('workload-shape'))}" data-briefing-section-content>
        <p>${topProjects.length
          ? `<span class="project-id-inline">${esc(topProjects[0][0])}${renderCopyIdButton(topProjects[0][0], 'copy-id-btn-sm')}</span> currently leads this snapshot with ${esc(String(topProjects[0][1]))} event${Number(topProjects[0][1]) === 1 ? '' : 's'}.`
          : 'Project and event concentration will render here once the dashboard has data.'}</p>
        <div class="briefing-shape-list">
          ${eventEntries.length
            ? eventEntries.slice(0, 4).map(([label, value]) => {
                const percent = payload.total_events ? Math.max(8, Math.round((Number(value || 0) / Math.max(1, Number(payload.total_events || 0))) * 100)) : 0;
                return `
                  <div class="briefing-shape-row">
                    <label>${esc(label)}</label>
                    <div class="briefing-shape-track"><div class="briefing-shape-fill" style="width:${percent}%"></div></div>
                    <span class="briefing-shape-value">${esc(String(value || 0))}</span>
                  </div>
                `;
              }).join('')
            : `<div class="briefing-empty">No event types recorded yet.</div>`}
        </div>
      </div>
    `;
  }

  renderWorkBriefingQuickStart();
  renderWorkBriefingRequestTemplates(payload);
  renderWorkBriefingDuplicateSignals(payload);

  document.getElementById('briefing-total-events').textContent = String(payload.total_events || 0);
  document.getElementById('briefing-selected-project').innerHTML = payload.project_id
    ? renderProjectIdWithCopy(payload.project_id, { extraClass: 'copy-id-btn-sm' })
    : 'all projects';
  document.getElementById('briefing-project-total').textContent = String(Object.keys(projectCounts).length);
  document.getElementById('briefing-project-counts').innerHTML = renderBriefingChips(projectCounts, 'No project activity yet');
  document.getElementById('briefing-event-types').innerHTML = renderBriefingChips(payload.event_type_counts || {}, 'No event types recorded');
  document.getElementById('briefing-status-counts').innerHTML = renderBriefingChips(payload.status_counts || {}, 'No statuses recorded');

  const briefingPane = document.getElementById('pane-briefing') || document;
  bindBriefingCollapseButtons(briefingPane);
  applyAllBriefingSectionCollapse();

  const tbody = document.getElementById('briefing-activities-body');
  const loadMoreBtn = document.getElementById('briefing-load-more-btn');
  const totalAvailable = Math.max(Number(payload.total_available || 0), activities.length);
  const hasMore = !!payload.has_more || totalAvailable > activities.length;
  renderWorkBriefingActivityControls(activities, filteredActivities.length, totalAvailable, activityCriteria);
  if (!activities.length) {
    tbody.innerHTML = `<tr><td colspan="4"><div class="empty-state">No work activity found for the current scope</div></td></tr>`;
    if (loadMoreBtn) loadMoreBtn.hidden = true;
    _workBriefingRenderSignature = currentSignature;
    if (preserveViewport) {
      if (tableWrap && tableScrollTop != null) tableWrap.scrollTop = tableScrollTop;
      if (duplicateWrap && duplicateScrollTop != null) duplicateWrap.scrollTop = duplicateScrollTop;
      if (scrollY != null) window.scrollTo(0, scrollY);
    }
    return;
  }
  if (!filteredActivities.length) {
    tbody.innerHTML = `<tr><td colspan="4"><div class="empty-state">No loaded events match the current filters</div></td></tr>`;
    if (loadMoreBtn) {
      loadMoreBtn.hidden = !hasMore;
      loadMoreBtn.disabled = _workBriefingLoading;
      loadMoreBtn.textContent = hasMore
        ? `Load ${Math.min(WORK_BRIEFING_BATCH_SIZE, Math.max(0, totalAvailable - activities.length)) || WORK_BRIEFING_BATCH_SIZE} more (${activities.length}/${totalAvailable})`
        : `All ${activities.length} loaded`;
    }
    _workBriefingRenderSignature = currentSignature;
    if (preserveViewport) {
      if (tableWrap && tableScrollTop != null) tableWrap.scrollTop = tableScrollTop;
      if (duplicateWrap && duplicateScrollTop != null) duplicateWrap.scrollTop = duplicateScrollTop;
      if (scrollY != null) window.scrollTo(0, scrollY);
    }
    return;
  }

  tbody.innerHTML = filteredActivities.map(activity => {
    const workspaceLabel = activity.workspace_name || '';
    const summaryText = activity.summary || activity.body_text || '';
    const occurredAt = activity.occurred_at || '';
    const activityTags = activity.suspected_duplicate
      ? ['possible-duplicate', ...(Array.isArray(activity.tags) ? activity.tags : [])]
      : activity.tags;
    const projectCell = activity.project_id
      ? `<div class="briefing-activity-cell">
           <div class="row1">${renderProjectIdWithCopy(activity.project_id, { extraClass: 'copy-id-btn-sm' })}</div>
           ${workspaceLabel ? `<div class="row2" title="${esc(workspaceLabel)}">${esc(workspaceLabel)}</div>` : ''}
         </div>`
      : '—';
    return `
      <tr class="briefing-activity-row">
        <td style="text-align:left">${projectCell}</td>
        <td style="text-align:center">
          <div class="briefing-icon-stack">
            ${renderBriefingEventIcon(activity.event_type)}
            ${renderBriefingStatusText(activity.status)}
          </div>
        </td>
        <td>
          <div class="briefing-activity-text">
            <strong title="${esc(activity.title || '')}">${esc(activity.title || 'Untitled activity')}</strong>
            ${summaryText ? `<div class="briefing-activity-summary" title="${esc(summaryText)}">${esc(summaryText)}</div>` : ''}
            ${renderBriefingTags(activityTags)}
          </div>
        </td>
        <td class="briefing-when-cell" title="${esc(occurredAt)}">
          <div>${esc(relativeTimeLabel(occurredAt))}</div>
          ${(() => {
            const pdt = formatPdtTimestampParts(occurredAt);
            if (!pdt) return '';
            return `<div class="briefing-when-stamp">${esc(pdt.date)}</div>
            <div class="briefing-when-stamp">${esc(pdt.time)}</div>
            <div class="briefing-when-stamp">${esc(pdt.tz)}</div>`;
          })()}
        </td>
      </tr>
    `;
  }).join('');

  if (loadMoreBtn) {
    loadMoreBtn.hidden = !hasMore;
    loadMoreBtn.disabled = _workBriefingLoading;
    loadMoreBtn.textContent = hasMore
      ? `Load ${Math.min(WORK_BRIEFING_BATCH_SIZE, Math.max(0, totalAvailable - activities.length)) || WORK_BRIEFING_BATCH_SIZE} more (${activities.length}/${totalAvailable})`
      : `All ${activities.length} loaded`;
  }
  _workBriefingRenderSignature = currentSignature;
  if (preserveViewport) {
    if (tableWrap && tableScrollTop != null) tableWrap.scrollTop = tableScrollTop;
    if (duplicateWrap && duplicateScrollTop != null) duplicateWrap.scrollTop = duplicateScrollTop;
    if (scrollY != null) window.scrollTo(0, scrollY);
  }
}

function renderWorkBriefingDuplicateSignals(payload) {
  const summaryEl = document.getElementById('briefing-duplicates-summary');
  const tbody = document.getElementById('briefing-duplicates-body');
  if (!summaryEl || !tbody) return;

  const groups = Array.isArray(payload?.suspected_duplicates) ? payload.suspected_duplicates : [];
  if (!groups.length) {
    summaryEl.textContent = 'No duplicate signals detected in the current dashboard slice.';
    tbody.innerHTML = `<tr><td colspan="4"><div class="empty-state">No duplicate clusters detected.</div></td></tr>`;
    return;
  }

  summaryEl.textContent = `${groups.length} duplicate signal ${groups.length === 1 ? 'group' : 'groups'} detected in the current slice.`;
  tbody.innerHTML = groups.slice(0, 10).map(group => {
    const projectCell = group.project_id
      ? renderProjectIdWithCopy(group.project_id, { extraClass: 'copy-id-btn-sm' })
      : '—';
    const sourceHint = Array.isArray(group.source_item_ids) && group.source_item_ids.length
      ? `<div class="briefing-activity-summary" title="${esc(group.source_item_ids.join(', '))}">sources: ${esc(group.source_item_ids.slice(0, 3).join(', '))}${group.source_item_ids.length > 3 ? ' ...' : ''}</div>`
      : '';
    const memberDetails = Array.isArray(group.members) && group.members.length
      ? `
        <details style="margin-top:6px">
          <summary style="cursor:pointer;color:var(--muted);font-size:12px">Show member details</summary>
          <div style="margin-top:6px;display:grid;gap:6px">
            ${group.members.map(member => `
              <div style="border:1px solid var(--border);border-radius:6px;padding:6px 8px;background:rgba(10,14,22,.55)">
                <div style="font-size:11px;color:var(--muted)">source: ${esc(member.source_item_id || '—')}</div>
                <div style="font-size:11px;color:var(--muted)">hash: ${esc(member.content_hash || '—')}</div>
                <div style="font-size:11px;color:var(--muted)">occurred: ${esc(member.occurred_at || '—')} · synced: ${esc(member.synced_at || '—')}</div>
                ${member.summary ? `<div class="briefing-activity-summary" title="${esc(member.summary)}">${esc(member.summary)}</div>` : ''}
              </div>
            `).join('')}
          </div>
        </details>
      `
      : '';
    return `
      <tr class="briefing-activity-row">
        <td style="text-align:left">${projectCell}</td>
        <td style="text-align:center">
          <div class="briefing-icon-stack">
            ${renderBriefingEventIcon(group.event_type)}
            <span class="briefing-status-dot is-open" title="duplicate signal" aria-label="duplicate signal"></span>
          </div>
        </td>
        <td>
          <div class="briefing-activity-text">
            <strong title="${esc(group.title || '')}">${esc(group.title || 'Duplicate signal')}</strong>
            <div class="briefing-activity-summary">${esc(group.reason || 'Possible duplicate activity cluster')} (${esc(String(group.duplicate_count || 0))} records)</div>
            ${sourceHint}
            ${memberDetails}
          </div>
        </td>
        <td class="briefing-when-cell" title="${esc(group.occurred_on || '')}">
          <div>${esc(group.occurred_on || '—')}</div>
        </td>
      </tr>
    `;
  }).join('');
}

async function loadBriefingPlugin(options = {}) {
  const append = !!options?.append;
  const preserveLimit = !!options?.preserveLimit;
  if (_workBriefingLoading) return;
  _workBriefingLoading = true;
  const tbody = document.getElementById('briefing-activities-body');
  const loadMoreBtn = document.getElementById('briefing-load-more-btn');
  if (tbody && !append) {
    if (!preserveLimit) {
      tbody.innerHTML = `<tr><td colspan="4"><div class="empty-state">Loading dashboard…</div></td></tr>`;
    }
  }
  if (loadMoreBtn) {
    loadMoreBtn.disabled = true;
    if (append) loadMoreBtn.textContent = 'Loading…';
  }

  try {
    const rawProjectInput = document.getElementById('briefing-project-id')?.value.trim() || '';
    // Input value may be a project name (highlighted in datalist) — resolve to project_id.
    const projectId = _projectIdByName[rawProjectInput] || rawProjectInput;
    const configuredBatch = Math.max(1, Math.min(Number(document.getElementById('briefing-limit')?.value || WORK_BRIEFING_BATCH_SIZE), WORK_BRIEFING_BATCH_SIZE));
    if ((!append && !preserveLimit) || projectId !== _workBriefingCurrentProjectId) {
      _workBriefingRequestedLimit = configuredBatch;
      _workBriefingCurrentProjectId = projectId;
    }
    const limit = Math.max(1, Math.min(_workBriefingRequestedLimit, 5000));
    const params = new URLSearchParams({ limit: String(limit) });
    if (projectId) params.set('project_id', projectId);
    _workBriefingSummary = await api('GET', '/admin/work-briefing?' + params.toString());
    renderWorkBriefing(_workBriefingSummary, { preserveViewport: preserveLimit });
  } catch (e) {
    toast(e.message, 'error');
    renderWorkBriefing();
  } finally {
    _workBriefingLoading = false;
  }
}
