'use strict';

const TOPBAR_NAV_ICONS = Object.freeze({
  'tab-briefing-btn':'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  'tab-adc-btn':'M4 3h11l5 5v13H4z M14 3v6h6 M8 13h8 M8 17h6',
  'tab-projects-btn':'M3 7V4h7l2 3h9v13H3z',
  'tab-extensions-btn':'M4 4h6a2 2 0 1 1 4 0h6v6a2 2 0 1 0 0 4v6h-6a2 2 0 1 0-4 0H4v-6a2 2 0 1 0 0-4z',
  'tab-schedule-btn':'M3 5h18v16H3z M7 2v6 M17 2v6 M3 10h18 M7 14h3 M14 14h3 M7 18h3',
  'tab-users-btn':'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M18 4a4 4 0 0 1 0 7 M22 21v-2a4 4 0 0 0-3-3.9',
  'tab-audit-btn':'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z M8 12l3 3 5-6',
  'tab-viewer-btn':'M6 4a2 2 0 1 0 0 4 2 2 0 0 0 0-4 M18 8a2 2 0 1 0 0 4 2 2 0 0 0 0-4 M9 17a2 2 0 1 0 0 4 2 2 0 0 0 0-4 M8 7l8 2 M6 8l2 9 M17 12l-6 6',
  'tab-settings-btn':'M10 3h4l1 3 3-1 2 3-2 2 3 2-1 4h-3l-1 3h-4l-1-3H7l-2-3 2-2-2-3 2-3 3 1z M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6',
});

let topbarNavigation = null;

function hideTopbarTooltip() {
  const state = topbarNavigation;
  state.tooltip.hidden = true;
  state.tooltipButton?.removeAttribute('aria-describedby');
  state.tooltipButton = null;
}

function showTopbarTooltip(button) {
  const state = topbarNavigation;
  hideTopbarTooltip();
  if (!state.nav.classList.contains('is-compact') || !button.getClientRects().length) return;
  state.tooltip.textContent = button.getAttribute('aria-label');
  state.tooltip.hidden = false;
  state.tooltipButton = button;
  button.setAttribute('aria-describedby', state.tooltip.id);
  const rect = button.getBoundingClientRect();
  const width = state.tooltip.getBoundingClientRect().width;
  state.tooltip.style.left = `${Math.max(8, Math.min(rect.left + rect.width / 2 - width / 2, document.documentElement.clientWidth - width - 8))}px`;
  state.tooltip.style.top = `${rect.bottom + 8}px`;
}

function updateTopbarNavigation() {
  const {nav, bar, buttons} = topbarNavigation;
  if (!bar.clientWidth) {
    hideTopbarTooltip();
    return;
  }
  const visible = buttons.filter(button => button.getClientRects().length);
  const style = getComputedStyle(bar);
  const number = value => parseFloat(value) || 0;
  const children = [...bar.children].filter(child => getComputedStyle(child).display !== 'none');
  let occupied = number(style.paddingLeft) + number(style.paddingRight)
    + number(style.columnGap) * Math.max(0, children.length - 1);
  for (const child of children) {
    const childStyle = getComputedStyle(child);
    occupied += number(childStyle.marginLeft) + number(childStyle.marginRight);
    if (child !== nav && !child.classList.contains('topbar-spacer')) occupied += child.getBoundingClientRect().width;
  }
  const navStyle = getComputedStyle(nav);
  let required = number(navStyle.columnGap) * Math.max(0, visible.length - 1);
  const padding = number(navStyle.getPropertyValue('--nav-text-padding'));
  for (const button of visible) {
    const buttonStyle = getComputedStyle(button);
    required += button.querySelector('.nav-label').getBoundingClientRect().width + padding * 2
      + 24
      + number(buttonStyle.borderLeftWidth) + number(buttonStyle.borderRightWidth);
  }
  nav.classList.toggle('is-compact', required > bar.clientWidth - occupied + 0.5);
  const focused = topbarNavigation.tooltipButton;
  if (focused && document.activeElement === focused) showTopbarTooltip(focused);
  else hideTopbarTooltip();
}

function initializeTopbarNavigation() {
  const nav = document.querySelector('.topbar-nav');
  const bar = nav.closest('.topbar');
  const buttons = [...nav.querySelectorAll('.tab-btn')];
  const tooltip = document.createElement('div');
  tooltip.className = 'topbar-nav-tooltip';
  tooltip.id = 'topbar-nav-tooltip';
  tooltip.setAttribute('role', 'tooltip');
  tooltip.hidden = true;
  document.body.append(tooltip);
  topbarNavigation = {nav, bar, buttons, tooltip, tooltipButton:null};
  for (const button of buttons) {
    const label = document.createElement('span');
    label.className = 'nav-label';
    label.textContent = button.textContent.trim();
    button.setAttribute('aria-label', label.textContent);
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('class', 'nav-icon');
    icon.setAttribute('width', '18');
    icon.setAttribute('height', '18');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('fill', 'none');
    icon.setAttribute('stroke', 'currentColor');
    icon.setAttribute('stroke-width', '1.7');
    icon.setAttribute('stroke-linecap', 'round');
    icon.setAttribute('stroke-linejoin', 'round');
    icon.setAttribute('aria-hidden', 'true');
    icon.setAttribute('focusable', 'false');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    if (!TOPBAR_NAV_ICONS[button.id]) throw new Error(`Missing navigation icon: ${button.id}`);
    path.setAttribute('d', TOPBAR_NAV_ICONS[button.id]);
    icon.append(path);
    button.replaceChildren(icon, label);
    button.addEventListener('pointerenter', () => showTopbarTooltip(button));
    button.addEventListener('pointerleave', () => {
      if (document.activeElement !== button) hideTopbarTooltip();
    });
    button.addEventListener('focus', () => showTopbarTooltip(button));
    button.addEventListener('blur', hideTopbarTooltip);
    button.addEventListener('keydown', event => {
      if (event.key === 'Escape') hideTopbarTooltip();
    });
    button.addEventListener('click', hideTopbarTooltip);
  }
  let pending = false;
  const schedule = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; updateTopbarNavigation(); });
  };
  const resize = new ResizeObserver(schedule);
  resize.observe(bar);
  for (const child of bar.children) if (child !== nav) resize.observe(child);
  const changes = new MutationObserver(schedule);
  changes.observe(nav, {subtree:true, attributes:true, attributeFilter:['style','hidden','class']});
  const onScroll = () => {
    const focused = buttons.find(button => button === document.activeElement);
    if (focused) showTopbarTooltip(focused);
    else hideTopbarTooltip();
  };
  nav.addEventListener('scroll', onScroll, {passive:true});
  window.addEventListener('resize', schedule, {passive:true});
  window.addEventListener('scroll', onScroll, {passive:true});
  document.fonts.ready.then(schedule);
  schedule();
}

initializeTopbarNavigation();

const ACTION_ICON_PATHS = Object.freeze({
  save: 'M4 3h13l4 4v14H3V3z M7 3v6h10V3 M7 21v-8h10v8',
  refresh: 'M20 7v5h-5 M4 17v-5h5 M6 6a8 8 0 0 1 14 6 M4 12a8 8 0 0 0 14 6',
  add: 'M12 4v16 M4 12h16',
  delete: 'M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7',
  close: 'M5 5l14 14 M19 5L5 19',
  copy: 'M8 8h13v13H8z M16 8V3H3v13h5',
  download: 'M12 3v12 M6 9l6 6 6-6 M3 16v5h18v-5',
  upload: 'M12 16V4 M6 10l6-6 6 6 M3 17v4h18v-4',
  run: 'M8 4l12 8-12 8z',
  inspect: 'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6',
  open: 'M14 3h7v7 M21 3l-11 11 M10 3H3v18h18v-7',
  back: 'M19 12H5 M12 5l-7 7 7 7',
  next: 'M5 12h14 M12 5l7 7-7 7',
  edit: 'M4 16L16 4l4 4L8 20H4z M14 6l4 4',
  profile: 'M20 21v-2a7 7 0 0 0-14 0v2 M13 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8',
  logout: 'M9 3H3v18h6 M14 8l4 4-4 4 M8 12h10',
  lock: 'M5 10h14v11H5z M8 10V6a4 4 0 0 1 8 0v4',
  stop: 'M6 5h4v14H6z M14 5h4v14h-4z',
  sort: 'M8 3v18 M4 17l4 4 4-4 M16 21V3 M12 7l4-4 4 4',
  expand: 'M5 9l7 7 7-7',
  theme: 'M20 14A8 8 0 0 1 10 4 8 8 0 1 0 20 14',
  connect: 'M8 3v5 M16 3v5 M5 8h14v3a7 7 0 0 1-14 0z M12 18v4',
  disconnect: 'M3 3l18 18 M8 3v2 M16 3v5 M11 8h8v3a7 7 0 0 1-2 5 M5 8v3a7 7 0 0 0 7 7v4',
  signal: 'M2 12h4l3-8 6 16 3-8h4',
  check: 'M4 12l5 5L20 6',
  database: 'M3 6c0-5 18-5 18 0s-18 5-18 0 M3 6v12c0 5 18 5 18 0V6 M3 12c0 5 18 5 18 0',
  history: 'M3 4v6h6 M3 10a9 9 0 1 1 1 9 M12 7v5l3 2',
  schedule: TOPBAR_NAV_ICONS['tab-schedule-btn'],
  settings: TOPBAR_NAV_ICONS['tab-settings-btn'],
});

function actionIconName(button) {
  const text = `${button.getAttribute('aria-label') || ''} ${button.title || ''} ${button.textContent} ${button.getAttribute('onclick') || ''}`.toLowerCase();
  for (const [pattern, name] of [
    [/save|apply|confirm|submit|accept/, 'save'], [/refresh|reload|retry|reset|restore|reindex|sync|loading|saving|running|connecting/, 'refresh'],
    [/disconnect/, 'disconnect'], [/connect/, 'connect'], [/record|signal|ai.first/, 'signal'],
    [/database|graphdb/, 'database'], [/security/, 'lock'], [/query strategy/, 'inspect'],
    [/change.?logs|version history/, 'history'], [/upgrade|update v/, 'upload'], [/load.*more/, 'download'], [/enable|select all/, 'check'],
    [/delete|remove|uninstall|clear|purge|revoke/, 'delete'], [/copy/, 'copy'],
    [/download|export|backup/, 'download'], [/upload|import|install/, 'upload'],
    [/openprofile|toggleusermenu/, 'profile'], [/logout|sign.?out/, 'logout'],
    [/cancel|close|dismiss/, 'close'], [/back|previous/, 'back'], [/next|forward/, 'next'],
    [/theme|dark|light mode/, 'theme'], [/pause|stop|disable/, 'stop'],
    [/password|login|sign.?in|token|secret|auth/, 'lock'], [/sort|ascending|descending/, 'sort'],
    [/collapse|expand|toggle|show|hide/, 'expand'], [/new|create|add|invite/, 'add'],
    [/edit|rename/, 'edit'], [/run|execute|generate|start|scan/, 'run'],
    [/check|test|search|filter|inspect|verify|validate|preview|review/, 'inspect'],
    [/open|view|browse|launch|history|report|log/, 'open'],
  ]) if (pattern.test(text)) return name;
  return 'settings';
}

function decorateActionButtons(root) {
  const buttons = root.matches?.('button') ? [root] : [...(root.querySelectorAll?.('button') || [])];
  for (const button of buttons) {
    if (button.querySelector('svg') || button.closest('.topbar-nav')) continue;
    const name = actionIconName(button);
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [key, value] of Object.entries({
      class: 'action-icon', width: '16', height: '16', viewBox: '0 0 24 24', fill: 'none',
      stroke: 'currentColor', 'stroke-width': '1.7', 'stroke-linecap': 'round',
      'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false',
    })) icon.setAttribute(key, value);
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', TOPBAR_NAV_ICONS[button.id] || ACTION_ICON_PATHS[name]);
    icon.append(path);
    button.prepend(icon);
    if (!button.textContent.trim()) {
      const label = button.getAttribute('aria-label') || button.title || name;
      button.setAttribute('aria-label', label);
      button.title ||= label;
    }
  }
}

decorateActionButtons(document.body);
const actionButtonsObserver = new MutationObserver(records => {
  for (const record of records) {
    const button = record.target.closest?.('button');
    if (button) decorateActionButtons(button);
    for (const node of record.addedNodes) if (node.nodeType === 1) decorateActionButtons(node);
  }
});
actionButtonsObserver.observe(document.body, {subtree: true, childList: true});
