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
    // The value text lines up with the axis labels, which ECharts draws 8 px
    // from the axis line: right-aligned on the left axis, left-aligned on the
    // right one. INSET = input padding + border.
    const INSET = 5, GAP = 8;
    const left = side === 'L' ? 0 : rect.x + rect.width + GAP - INSET;
    const w = side === 'L' ? Math.max(40, rect.x - GAP + INSET) : Math.max(40, total - left - 10);
    for (const ctl of [minCtl, maxCtl]) {
      ctl.style.width = w + 'px';
      ctl.style.left = left + 'px';
      ctl.classList.toggle('y-axis-ctl--right', side === 'R');
    }
    maxCtl.style.top = (rect.y - 11) + 'px';
    minCtl.style.top = (rect.y + rect.height - 11) + 'px';
  }
}

// ─── Toolbar shared by the Live Chart and History ──────────────────────────────
function setToggle(id, on) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('active', !!on);
}

function confirmUnselect(n) {
  return n > 0 && confirm(`Remove all ${n} signal${n === 1 ? '' : 's'} from the chart?`);
}

// Saved views: dropdown, Load / delete / embed buttons. Uses the page's setStatus().
function _onViewSelChange() {
  const id = document.getElementById('loadViewSel').value;
  for (const btn of ['loadViewBtn', 'deleteViewBtn', 'openEmbedBtn']) {
    const el = document.getElementById(btn);
    if (el) el.disabled = !id;
  }
}

function openEmbedView() {
  const id = document.getElementById('loadViewSel').value;
  if (id) window.open('/chart/embed/' + id, '_blank', 'noopener');
}

async function _refreshViewDropdown() {
  const sel = document.getElementById('loadViewSel');
  const cur = sel.value;
  try {
    const list = await (await fetch('/chart/configs')).json();
    sel.innerHTML = '<option value="">Select…</option>';
    for (const v of list) {
      const opt = document.createElement('option');
      opt.value = v.id;
      opt.textContent = v.name;
      sel.appendChild(opt);
    }
    if (cur && sel.querySelector(`option[value="${cur}"]`)) sel.value = cur;
  } catch {}
  _onViewSelChange();
}

async function deleteSelectedView() {
  const sel = document.getElementById('loadViewSel');
  const id = sel.value;
  if (!id) return;
  if (!confirm('Delete view "' + (sel.options[sel.selectedIndex]?.text || id) + '"?')) return;
  try {
    await fetch('/chart/configs/' + id + '/delete', {method: 'POST'});
    await _refreshViewDropdown();
    setStatus('View deleted.');
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

// Legend row parts shared by the Live Chart and History: "×" with the display
// multiplier input, and the remove button.
function legendMultiplier(key, mul, onInput) {
  const x = document.createElement('span');
  x.className = 'text-muted';
  x.style.cssText = 'font-size:.82rem;flex-shrink:0';
  x.textContent = '×';
  const inp = document.createElement('input');
  inp.type = 'number'; inp.step = '0.1'; inp.min = '0.001';
  inp.value = Number.isInteger(mul * 10) ? mul.toFixed(1) : mul;
  inp.dataset.mulKey = key;
  inp.className = 'input input-sm';
  inp.style.cssText = 'width:58px;padding:1px 4px;font-size:.82rem;text-align:right';
  inp.title = 'Scale multiplier for chart display';
  inp.addEventListener('input', () => onInput(inp.value));
  return [x, inp];
}

function legendRemoveButton(onClick) {
  const rm = document.createElement('button');
  rm.type = 'button';
  rm.className = 'btn btn-ghost btn-xs';
  rm.style.cssText = 'padding:2px 6px;color:var(--red,#ef4444);flex-shrink:0';
  rm.title = 'Remove signal';
  rm.textContent = '✕';
  rm.addEventListener('click', onClick);
  return rm;
}
