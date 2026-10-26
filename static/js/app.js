/**
 * OpcUA_2_InfluxDB — browser-side helpers
 * No framework, no build step. Vanilla JS only.
 */

// ---------------------------------------------------------------------------
// Tree node: subscribe/unsubscribe via HTMX (click = immediate server action)
// ---------------------------------------------------------------------------

/**
 * Shows a 4-option modal for the Name Strip Prefix choice when creating a
 * dynamic folder watcher. Resolves to 'full', 'short', 'none', or 'cancel'.
 *   - full:   strip the entire folder path (includes the folder's own name)
 *   - short:  strip everything except the folder's own name — keeps the folder
 *             name in the measurement so multiple dynamic folders sharing a
 *             common root stay distinguishable
 *   - none:   no prefix — subscriptions use the full node path as-is
 *   - cancel: abort — do not create the watcher at all (also triggered by
 *             clicking outside the dialog)
 */
function askPrefixChoice(fullPrefix, shortPrefix) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML =
      '<div class="modal-card">' +
        '<h3 style="margin:0 0 .5rem;font-size:1rem">Name Strip Prefix</h3>' +
        '<p class="text-sm text-muted" style="margin:0 0 1rem">' +
          'Set this now, before the first scan — InfluxDB can\'t retroactively rename ' +
          'measurements already written, so adding a prefix later would split your ' +
          'history across the long and short names.' +
        '</p>' +
        '<div class="modal-prefix-line"><span class="text-xs text-muted">Full path:</span> <code>' + (fullPrefix || '(empty)') + '</code></div>' +
        '<div class="modal-prefix-line" style="margin-bottom:1rem"><span class="text-xs text-muted">Without folder name:</span> <code>' + (shortPrefix || '(empty)') + '</code></div>' +
        '<button class="btn btn-ghost modal-choice" data-choice="full">Strip full path</button>' +
        '<button class="btn btn-ghost modal-choice" data-choice="short">Keep folder name out of prefix</button>' +
        '<button class="btn btn-ghost modal-choice" data-choice="none">No trim — use full node paths</button>' +
        '<button class="btn btn-ghost modal-choice" data-choice="cancel" style="color:var(--red,#ef4444)">Do not subscribe</button>' +
      '</div>';
    overlay.addEventListener('click', (e) => {
      const choiceBtn = e.target.closest('button[data-choice]');
      if (choiceBtn) {
        overlay.remove();
        resolve(choiceBtn.dataset.choice);
      } else if (e.target === overlay) {
        overlay.remove();
        resolve('cancel');
      }
    });
    document.body.appendChild(overlay);
  });
}

/**
 * Handles a "Subscribe dynamically" click. If the folder is being turned ON
 * (not OFF), asks — BEFORE the initial scan runs — whether/how to add a
 * matching Name Strip Prefix for this server. Only after that (optional) step
 * does the actual subscribe/scan request fire.
 */
async function onDynFolderClick(btn) {
  const serverId     = btn.dataset.serverId;
  const nodeId       = btn.dataset.nodeId;
  const browsePath   = btn.dataset.browsePath || '';
  const displayName  = btn.dataset.displayName || '';
  const wasDynamic   = btn.dataset.wasDynamic === 'true';
  const targetId     = btn.dataset.target;

  if (!wasDynamic) {
    const segments    = browsePath.split('/').filter(Boolean);
    const fullPrefix  = segments.join('.');
    const shortPrefix = segments.slice(0, -1).join('.');
    if (fullPrefix) {
      const choice = await askPrefixChoice(fullPrefix, shortPrefix);
      if (choice === 'cancel') return; // abort — do not create the watcher
      const prefix = choice === 'full' ? fullPrefix : choice === 'short' ? shortPrefix : '';
      if (prefix) {
        await fetch('/config/servers/' + encodeURIComponent(serverId) + '/name-strip-prefixes', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: new URLSearchParams({prefix}),
        });
      }
    }
  }

  btn.disabled = true;
  try {
    const resp = await fetch('/browser/subscribe-folder-dynamic', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams({
        server_id: serverId, node_id: nodeId,
        browse_path: browsePath, display_name: displayName,
      }),
    });
    const html = await resp.text();
    const target = document.getElementById(targetId);
    if (target) target.innerHTML = html;
    // Flip the button to reflect the new watcher state immediately
    const nowDynamic = !wasDynamic;
    btn.dataset.wasDynamic = nowDynamic ? 'true' : 'false';
    btn.textContent = nowDynamic ? '✕⟳' : '⟳';
    btn.title = nowDynamic
      ? 'Remove dynamic subscription watcher'
      : 'Subscribe dynamically — auto-add new nodes, disable removed ones';
    btn.classList.toggle('btn-dyn-folder--active', nowDynamic);
    // Lock/unlock this folder's own checkbox and re-render any loaded children
    // so their checkboxes pick up the new locked/unlocked state immediately.
    const li = btn.closest('.node');
    const folderCb = li && li.querySelector(':scope > .node-header > .folder-checkbox');
    if (folderCb) {
      folderCb.disabled = nowDynamic;
      folderCb.checked = nowDynamic ? true : folderCb.checked;
      folderCb.classList.toggle('subscribed--dyn', nowDynamic);
      folderCb.title = nowDynamic
        ? 'Managed by a dynamic folder watcher — release the watcher (✕⟳) to change subscriptions manually'
        : 'Empty: click to subscribe all children · Partial or full: click to unsubscribe all';
      if (nowDynamic) folderCb.dataset.dynLocked = '1';
      else delete folderCb.dataset.dynLocked;
    }
    const childrenUl = li && li.querySelector(':scope > .node-children');
    const toggleBtn = li && li.querySelector(':scope > .node-header > .node-toggle');
    if (childrenUl && toggleBtn && childrenUl.innerHTML.trim() && toggleBtn.getAttribute('hx-get')) {
      const url = toggleBtn.getAttribute('hx-get');
      const valsStr = toggleBtn.getAttribute('hx-vals');
      const params = new URLSearchParams(valsStr ? JSON.parse(valsStr) : {});
      try {
        const r = await fetch(url + '?' + params.toString());
        const childHtml = await r.text();
        if (childHtml.trim()) {
          childrenUl.innerHTML = childHtml;
          htmx.process(childrenUl);
          applyFolderHints();
        }
      } catch { /* children keep their previous rendering until next expand */ }
    }
  } finally {
    btn.disabled = false;
  }
}

/** Called when a Variable node checkbox changes — update parent folder tri-state. */
function onVariableCheckChange(checkbox) {
  let parent = checkbox.closest('.node-children');
  while (parent) {
    const li = parent.closest('.node');
    if (!li) break;
    const folderCb = li.querySelector(':scope > .node-header > .folder-checkbox');
    if (folderCb) updateFolderCheckbox(folderCb);
    parent = li.closest('.node-children');
  }
}

/**
 * Called when a folder checkbox changes.
 * Sends a recursive subscribe/unsubscribe request to the server, then reloads
 * the folder's children to reflect the new subscription state.
 * The folder is NOT expanded if it was already collapsed.
 */
function onFolderCheckChange(folderCb) {
  const li = folderCb.closest('.node');
  if (!li) return;

  // Clicking a partially-subscribed (indeterminate) folder fully unsubscribes it.
  // Native checkboxes turn an indeterminate box into "checked" on click, so use the
  // prior partial state (dataset.partial) to override the action to unsubscribe.
  const wasPartial = folderCb.dataset.partial === '1';
  let wantChecked = folderCb.checked;
  if (wasPartial) {
    wantChecked = false;
    folderCb.checked = false;
    folderCb.indeterminate = false;
  }
  delete folderCb.dataset.partial;

  const serverId = li.dataset.server;
  const nodeId = folderCb.dataset.nodeId || '';        // '' = server root
  const browsePath = folderCb.dataset.path || '';
  if (!serverId) return;

  const action = wantChecked ? 'subscribe-folder' : 'unsubscribe-folder';
  folderCb.disabled = true;

  fetch('/browser/' + action, {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: new URLSearchParams({server_id: serverId, node_id: nodeId, browse_path: browsePath})
  }).then(() => {
    folderCb.disabled = false;

    if (nodeId) {
      // Sub-folder: reload only this folder's children — preserves the rest of the tree.
      const childrenUl = li.querySelector(':scope > .node-children');
      const toggle = li.querySelector(':scope > .node-header > .node-toggle');
      const alreadyExpanded = toggle && toggle.classList.contains('expanded');
      if (childrenUl) {
        const params = new URLSearchParams({parent_path: browsePath});
        htmx.ajax(
          'GET',
          '/browser/nodes/' + serverId + '/' + encodeURIComponent(nodeId) + '?' + params.toString(),
          {target: childrenUl, swap: 'innerHTML'}
        );
        // Only show children if the folder was already expanded — do NOT auto-expand
        if (alreadyExpanded) childrenUl.style.display = 'block';
      }
    } else {
      // Server root: reload top-level children for this server.
      const serverLi = document.querySelector('.node--server[data-server="' + serverId + '"]');
      const reloadTarget = serverLi ? serverLi.querySelector(':scope > .node-children') : null;
      const toggle = serverLi ? serverLi.querySelector(':scope > .node-header > .node-toggle') : null;
      const alreadyExpanded = toggle && toggle.classList.contains('expanded');
      if (reloadTarget) {
        htmx.ajax('GET', '/browser/nodes/' + serverId, {target: reloadTarget, swap: 'innerHTML'});
        if (alreadyExpanded) reloadTarget.style.display = 'block';
      }
    }

    // Immediately update this folder's checkbox state and propagate upward.
    folderCb.checked = wantChecked;
    folderCb.indeterminate = false;
    _propagateFolderUp(li);
  }).catch(() => { folderCb.disabled = false; });
}

/** Walk up the tree from a node's <li> and refresh every ancestor folder checkbox. */
function _propagateFolderUp(li) {
  let parentContainer = li.closest('.node-children');
  while (parentContainer) {
    const parentLi = parentContainer.closest('.node');
    if (!parentLi) break;
    const parentFolderCb = parentLi.querySelector(':scope > .node-header > .folder-checkbox');
    if (parentFolderCb) updateFolderCheckbox(parentFolderCb);
    parentContainer = parentLi.closest('.node-children');
  }
}

/**
 * A folder is "resolved" once any of its descendant checkboxes are loaded into
 * the DOM. Before that we only have the server-side data-has-subs hint, which
 * tells us a folder has *some* subscribed descendant but not whether it's fully
 * or partially subscribed. Once resolved, the folder's own checked/indeterminate
 * state is authoritative and the hint must be ignored.
 */
function _folderResolved(folderCb) {
  const li = folderCb.closest('.node');
  if (!li) return false;
  // >1 because the folder's own checkbox is included in the query.
  return li.querySelectorAll('.node-checkbox').length > 1;
}

/** Update a folder checkbox tri-state based on subscribed children (all depths). */
function updateFolderCheckbox(folderCb) {
  // Watcher-locked folders are rendered checked+disabled server-side and must
  // not be recomputed from children state.
  if (folderCb.dataset.dynLocked) return;
  const li = folderCb.closest('.node');
  if (!li) return;

  // All variable checkboxes loaded anywhere in this subtree
  const varCbs = Array.from(li.querySelectorAll('.variable-checkbox'));
  // All child folder checkboxes (not this one) — includes loaded sub-folders at any depth
  const childFolderCbs = Array.from(li.querySelectorAll('.folder-checkbox')).filter(f => f !== folderCb);

  if (varCbs.length === 0 && childFolderCbs.length === 0) {
    // No loaded children — use server-side hint
    if (folderCb.dataset.hasSubs) {
      folderCb.indeterminate = true;
      folderCb.checked = false;
      folderCb.dataset.partial = '1';
    }
    return;
  }

  const checkedVars = varCbs.filter(cb => cb.checked).length;
  const allVarsChecked = varCbs.length === 0 || checkedVars === varCbs.length;
  const anyVarChecked  = checkedVars > 0;

  // A child folder contributes "any" if checked, indeterminate, or has data-has-subs hint
  // (data-has-subs means it has subscribed descendants even if not yet silently fetched)
  const anyFolderChecked = childFolderCbs.some(f => f.checked || f.indeterminate || !!f.dataset.hasSubs);
  // "all" only if every child is fully checked. A resolved child's own state is
  // authoritative (ignore its stale has-subs hint); an unresolved child with a
  // has-subs hint can't be assumed fully checked.
  const allFoldersFullyChecked = childFolderCbs.length === 0 ||
    childFolderCbs.every(f => f.checked && !f.indeterminate &&
      (_folderResolved(f) || !f.dataset.hasSubs));

  const allChecked = allVarsChecked && allFoldersFullyChecked;
  const anyChecked = anyVarChecked  || anyFolderChecked;

  folderCb.indeterminate = anyChecked && !allChecked;
  folderCb.checked       = allChecked && anyChecked;
  // Keep the accent color consistent with variable checkboxes: green once
  // everything below is subscribed, default blue otherwise.
  folderCb.classList.toggle('subscribed', folderCb.checked);
  // Record partial state so a click on this folder can mean "unsubscribe all"
  // (clicking a native indeterminate checkbox would otherwise turn it checked).
  if (folderCb.indeterminate) folderCb.dataset.partial = '1';
  else delete folderCb.dataset.partial;
}

/** Called when expand toggle is clicked — update chevron and show/hide children. */
function onToggleClick(btn) {
  const isExpanded = btn.classList.toggle('expanded');
  const li = btn.closest('.node');
  if (!li) return;
  const childrenUl = li.querySelector(':scope > .node-children');
  if (childrenUl) {
    childrenUl.style.display = isExpanded ? 'block' : 'none';
  }
}

// After any HTMX swap/settle, re-evaluate all folder checkboxes.
// Process bottom-up (deepest first): a parent's tri-state depends on its child
// folders' computed state, and child folders appear later in document order.
//
// Folders whose children are NOT loaded yet get their state instantly from the
// server-side data-has-subs hint (indeterminate) — deliberately NO silent
// background fetching of children: the old prefetch cascade hammered the PLC
// with one request chain per hinted folder and made the checkmarks appear
// only after seconds. Precise tri-state refinement happens naturally when the
// user actually expands a folder (its children are in the DOM then).
function applyFolderHints() {
  Array.from(document.querySelectorAll('.folder-checkbox')).reverse().forEach(function(folderCb) {
    const li = folderCb.closest('.node');
    if (!li) return;
    const varCbs = li.querySelectorAll('.variable-checkbox');
    if (varCbs.length > 0 || _folderResolved(folderCb)) {
      updateFolderCheckbox(folderCb);
      return;
    }
    if (folderCb.dataset.hasSubs) {
      folderCb.indeterminate = true;
      folderCb.dataset.partial = '1';
    }
  });
}
document.addEventListener('htmx:afterSwap', function(evt) {
  applyFolderHints();
  // Show/hide inline-edit <tr> rows (InfluxDB / SQLite config tables)
  if (evt.detail && evt.detail.target) {
    const el = evt.detail.target;
    if (el.tagName === 'TR' && el.id && (el.id.startsWith('edit-') || el.id.startsWith('retention-'))) {
      el.style.display = el.innerHTML.trim() ? '' : 'none';
    }
  }
});
document.addEventListener('htmx:afterSettle', function () {
  applyFolderHints();
  document.querySelectorAll('.auto-dismiss').forEach(el => {
    setTimeout(() => el.remove(), 2000);
  });
});

/** Reset all child toggle HTMX triggers inside a server's tree (called on ↻ refresh). */
function resetServerTree(serverId) {
  const serverLi = document.querySelector('.node--server[data-server="' + serverId + '"]');
  if (!serverLi) return;
  serverLi.querySelectorAll('.node-toggle').forEach(function(btn) {
    // Re-enable HTMX trigger so nodes can be re-fetched after server reconnect
    if (btn.getAttribute('hx-trigger') === 'none') {
      btn.setAttribute('hx-trigger', 'click once');
    }
  });
  // Also reset silentFetched guards on all child nodes
  serverLi.querySelectorAll('.node').forEach(function(li) {
    if (li.dataset.silentFetched) li.dataset.silentFetched = '';
  });
}

// On initial page load, apply hints and dismiss auto-dismiss alerts.
document.addEventListener('DOMContentLoaded', function () {
  applyFolderHints();
  document.querySelectorAll('.auto-dismiss').forEach(el => {
    setTimeout(() => el.remove(), 2000);
  });
});

// ---------------------------------------------------------------------------
// Config page helpers
// ---------------------------------------------------------------------------

function toggleAuthFields(sel) {
  const el = document.getElementById('auth-fields');
  if (el) el.style.display = sel.value === 'username' ? '' : 'none';
}

function toggleThreshold(sel) {
  const row = sel.closest('tr') || sel.closest('.form-row');
  if (!row) return;
  const thr = row.querySelector('[name="threshold"]');
  if (thr) thr.disabled = !['on_change_absolute', 'on_change_relative'].includes(sel.value);
}

// ---------------------------------------------------------------------------
// Toast notifications
// ---------------------------------------------------------------------------

function showToast(msg, type) {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.textContent = msg;
  toast.className = 'toast toast-' + (type || 'info') + ' show';
  setTimeout(() => { toast.className = 'toast'; }, 3000);
}
