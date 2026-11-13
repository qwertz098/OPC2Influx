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

// Manual Y axis range: two number inputs #yMinInp / #yMaxInp; an empty field
// scales that end automatically. Returns {min, max} with null for "auto".
function readYRange() {
  const num = id => {
    const el = document.getElementById(id);
    const v = el ? el.value.trim() : '';
    return v === '' || !isFinite(Number(v)) ? null : Number(v);
  };
  let min = num('yMinInp'), max = num('yMaxInp');
  const bad = min !== null && max !== null && min >= max;
  for (const id of ['yMinInp', 'yMaxInp']) {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('input-invalid', bad);
  }
  if (bad) min = max = null;   // contradictory: ignore both until fixed
  return {min, max};
}

function setYRange(min, max) {
  const put = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.value = v === null || v === undefined ? '' : String(v);
  };
  put('yMinInp', min);
  put('yMaxInp', max);
}

// ECharts yAxis limits: a fixed number, or null for automatic
function yLimits() {
  const r = readYRange();
  return {min: r.min, max: r.max};
}
