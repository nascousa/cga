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
