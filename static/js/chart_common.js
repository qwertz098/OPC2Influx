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
