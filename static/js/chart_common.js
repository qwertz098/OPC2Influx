// Shared by the Live Chart (templates/chart.html) and History (templates/history.html).
// Expects a global STRIP_PREFIXES = {server_id: [prefix, …]} defined by the page.
const COLORS = [
  '#e6194b','#3cb44b','#4363d8','#f58231','#911eb4',
  '#42d4f4','#f032e6','#bfef45','#469990','#dcbeff',
];

function sigKey(server_id, node_id) { return server_id + '|' + node_id; }

function browsePath2Label(path) {
  return path.split('/').filter(Boolean).join('.') || path;
}

function applyStrips(server_id, label) {
  const strips = STRIP_PREFIXES[server_id] || [];
  for (const s of strips) {
    if (s && label.startsWith(s)) {
      label = label.slice(s.length).replace(/^[./]+/, '');
    }
  }
  return label;
}

function sigLabel(sig) {
  if (sig.raw) return sig.label || sig.name; // already the actual measurement name — don't re-derive/strip
  return applyStrips(sig.server_id, sig.label || sig.name) || sig.name;
}

function fmtVal(v) {
  if (v === null || v === undefined) return '—';
  if (Number.isInteger(v)) return String(v);
  const abs = Math.abs(v);
  if (abs === 0) return '0';
  // Always use decimal notation — no scientific notation
  if (abs >= 100000) return v.toFixed(0);
  if (abs >= 10000)  return v.toFixed(1);
  if (abs >= 1000)   return v.toFixed(1);
  if (abs >= 100)    return v.toFixed(2);
  if (abs >= 10)     return v.toFixed(3);
  if (abs >= 1)      return v.toFixed(4);
  if (abs >= 0.01)   return v.toFixed(5);
  return v.toFixed(8);
}

// Subscriptions tile: text + server filter
function filterSubList() {
  const q = (document.getElementById('subFilterInput')?.value || '').trim().toLowerCase();
  const serverId = document.getElementById('subServerFilter')?.value || '';
  const items = document.querySelectorAll('#subList .sub-list-item');
  let visibleCount = 0;
  items.forEach(li => {
    const textMatch = !q || (li.dataset.search || '').includes(q);
    const serverMatch = !serverId || li.dataset.serverId === serverId;
    const match = textMatch && serverMatch;
    li.style.display = match ? '' : 'none';
    if (match) visibleCount++;
  });
  const empty = document.getElementById('subFilterEmpty');
  if (empty) empty.style.display = visibleCount === 0 ? '' : 'none';
}

// Colours of the active theme for ECharts, which draws on a canvas and cannot
// read CSS variables itself. Charts re-render on the 'themechange' event.
function chartColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = name => cs.getPropertyValue(name).trim();
  return {fg: v('--fg'), muted: v('--muted'), line: v('--line'), grid: v('--grid'),
          frame: v('--frame'), surface: v('--panel-raised'), accent: v('--accent')};
}

function themedAxis(c, axis) {
  return {
    ...axis,
    axisLine: {...(axis.axisLine || {}), lineStyle: {color: c.frame}},
    axisTick: {lineStyle: {color: c.frame}},
    axisLabel: {color: c.muted, ...(axis.axisLabel || {})},
    splitLine: {...{lineStyle: {color: c.grid}}, ...(axis.splitLine || {})},
  };
}

function themedTooltip(c, tooltip) {
  return {backgroundColor: c.surface, borderColor: c.line, textStyle: {color: c.fg}, ...tooltip};
}

function hexAlpha(hex, a) {
  const n = parseInt(hex.replace('#', ''), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

// ─── Y axes ──────────────────────────────────────────────────────────────────
// Each signal sits on the left (default) or right Y axis: sig.axis = 'L' | 'R'.
function sigSide(sig) { return sig && sig.axis === 'R' ? 'R' : 'L'; }
function usesRightAxis(signals) {
  for (const sig of signals.values()) if (sigSide(sig) === 'R') return true;
  return false;
}

// Two checkboxes L / R for a legend row; exactly one is ticked.
function axisPicker(sig, onChange) {
  const box = document.createElement('span');
  box.className = 'axis-pick';
  box.title = 'Y axis of this signal: left or right';
  for (const side of ['L', 'R']) {
    const lab = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = sigSide(sig) === side;
    cb.addEventListener('change', () => {
      sig.axis = side;              // ticking either box moves the signal there
      box.querySelectorAll('input').forEach((c, i) => { c.checked = ['L', 'R'][i] === side; });
      onChange();
    });
    lab.append(cb, side);
    box.appendChild(lab);
  }
  return box;
}

// Y axis end fields (#yMaxInp / #yMinInp, right axis with suffix R) take the
// place of the axis' top and bottom labels. In automatic mode they only show
// the current end value; a click switches that end to manual input
// (data-manual="1"), × switches it back.
function yInputs(side = 'L') {
  const sfx = side === 'R' ? 'R' : '';
  return [document.getElementById('yMinInp' + sfx), document.getElementById('yMaxInp' + sfx)];
}

function readYRange(side = 'L') {
  const num = el => {
    if (!el || el.dataset.manual !== '1') return null;
    const v = el.value.trim();
    return v === '' || !isFinite(Number(v)) ? null : Number(v);
  };
  const [minEl, maxEl] = yInputs(side);
  let min = num(minEl), max = num(maxEl);
  const bad = min !== null && max !== null && min >= max;
  for (const el of [minEl, maxEl]) if (el) el.classList.toggle('input-invalid', bad);
  if (bad) min = max = null;   // contradictory: ignore both until fixed
  return {min, max};
}

function setYManual(el, on) {
  if (!el) return;
  el.dataset.manual = on ? '1' : '';
  el.readOnly = !on;
  el.closest('.y-axis-ctl')?.classList.toggle('manual', on);
  el.title = on ? 'Manual Y axis end. Enter a value; × returns to automatic.'
                : 'Automatic Y axis end. Click to set it manually.';
}

function setYRange(min, max, side = 'L') {
  const [minEl, maxEl] = yInputs(side);
  for (const [el, v] of [[minEl, min], [maxEl, max]]) {
    if (!el) continue;
    const on = v !== null && v !== undefined && v !== '';
    setYManual(el, on);
    el.value = on ? String(v) : '';
  }
}

// Both axes as stored in page state: {L: {min, max}, R: {min, max}}
function readYRanges() { return {L: readYRange('L'), R: readYRange('R')}; }
function setYRanges(r) {
  r = r || {};
  // Older state had one {min, max} for the only axis
  const left = r.L || (('min' in r || 'max' in r) ? r : {});
  setYRange(left.min, left.max, 'L');
  setYRange((r.R || {}).min, (r.R || {}).max, 'R');
}

// Click on an automatic end: keep its current value and make it editable
function yManualOn(el) {
  if (el.dataset.manual === '1') return;
  setYManual(el, true);
  setTimeout(() => el.select(), 0);
  onYRangeChange();
}

function yManualOff(id) {
  const el = document.getElementById(id);
  setYManual(el, false);
  el.value = '';
  onYRangeChange();
}

// ECharts yAxis limits: a fixed number, or null for automatic
function yLimits(side = 'L') {
  const r = readYRange(side);
  return {min: r.min, max: r.max};
}

function fmtAxisEnd(v) {
  if (!isFinite(v)) return '';
  return String(Number(Math.abs(v) >= 1e6 || (v !== 0 && Math.abs(v) < 1e-3) ? v.toPrecision(3) : v.toPrecision(6)));
}

// Put the end fields where the axis' top and bottom labels would be and show
// the current end values in the automatic ones. axes: {L: yAxisIndex, R: yAxisIndex | null}
function placeYInputs(chart, axes = {L: 0, R: null}) {
  if (!chart) return;
  const model = chart.getModel();
  const grid = model && model.getComponent('grid', 0);
  const rect = grid && grid.coordinateSystem && grid.coordinateSystem.getRect();
  const hasSeries = !!(rect && rect.height > 0 && chart.getOption().series.length);
  const total = chart.getWidth();
  for (const side of ['L', 'R']) {
    const sfx = side === 'R' ? 'R' : '';
    const minCtl = document.getElementById('yMinCtl' + sfx), maxCtl = document.getElementById('yMaxCtl' + sfx);
    if (!minCtl || !maxCtl) continue;
    const idx = axes[side];
    const shown = hasSeries && idx !== null && idx !== undefined && !!model.getComponent('yAxis', idx);
    minCtl.hidden = maxCtl.hidden = !shown;
    if (!shown) continue;
    const ext = model.getComponent('yAxis', idx).axis.scale.getExtent();
    const [minEl, maxEl] = yInputs(side);
    for (const [el, v] of [[minEl, ext[0]], [maxEl, ext[1]]]) {
      if (el.dataset.manual !== '1' && document.activeElement !== el) el.value = fmtAxisEnd(v);
    }
    const left = side === 'L' ? 0 : rect.x + rect.width + 6;
    const w = side === 'L' ? Math.max(40, rect.x - 6) : Math.max(40, total - left - 10);
    for (const ctl of [minCtl, maxCtl]) { ctl.style.width = w + 'px'; ctl.style.left = left + 'px'; }
    maxCtl.style.top = (rect.y - 11) + 'px';
    minCtl.style.top = (rect.y + rect.height - 11) + 'px';
  }
}
