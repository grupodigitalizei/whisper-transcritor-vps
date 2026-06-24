// ═══════════════════════════════════════════════════════════════
//  STATE
// ═══════════════════════════════════════════════════════════════
let files    = [];     // [{id, file, name, date, dur, status, mode}]
let selected = new Set();
let pendingFiles = []; // File objects queued from input
let _viewerFile  = null;
let _viewerData  = {};
let _autoSyncInterval = null;
let _syncFingerprint  = '';

// ═══════════════════════════════════════════════════════════════
//  INIT
// ═══════════════════════════════════════════════════════════════
async function init() {
  await Promise.all([loadHistory(), loadStats(), loadFolders()]);
  await resumeActivePolling();
  startAutoSync();
  // Non-blocking — banner appears later if there are old media files to clean
  checkOldMediaCleanup();
}

async function resumeActivePolling() {
  try {
    // 1. Ask server which tasks are still alive in memory
    const res   = await fetch('/api/active-tasks');
    const tasks = await res.json(); // { task_id: {status, filename, ...} }

    // 2. Reset items stuck as queued/processing whose task is gone (server restart)
    const resetRes = await fetch('/api/reset-stale', { method: 'POST' });
    const resetData = await resetRes.json();
    if (resetData.reset > 0) {
      // Reload history to pick up newly "error" items
      await loadHistory();
    }

    // 3. Resume polling for any tasks that are still alive
    for (const [task_id, task] of Object.entries(tasks)) {
      const filename = task.filename || task.name;
      if (!filename || _activePolls[task_id]) continue;
      pollProgressForRow(task_id, filename);
    }
  } catch { /* server may not have active tasks */ }
}

// ─── helpers ─────────────────────────────────────────────────
function _historyToFiles(data) {
  // Carry over live progress (%) and phase from the current in-memory list so a
  // history refresh mid-transcription doesn't drop what's being displayed.
  const prev = new Map((files || []).map(f => [f.file, {
    _progress: f._progress, _phase: f._phase, _phaseProgress: f._phaseProgress,
  }]));
  return data.map(h => {
    const carried = prev.get(h.file) || {};
    return {
      id:      h.file,
      file:    h.file,
      name:    h.name || h.file.replace(/\.[^.]+$/, ''),
      date:    h.date    || '—',
      dur:     h.duration || '—',
      dur_secs: h.duration_secs || 0,
      status:  h.status   || 'done',
      mode:    h.mode     || 'turbo',
      lang:    h.lang     || '',
      words:   h.words    || 0,
      error:   h.error    || null,
      task_id: h.task_id  || null,
      folder:  h.folder   || '',
      queued_at:      h.queued_at      || 0,
      started_at:     h.started_at     || null,
      completed_at:   h.completed_at   || null,
      processing_secs: h.processing_secs || null,
      source:         h.source         || 'upload',  // 'upload' | 'url'
      url:            h.url            || null,       // original link (yt-dlp), if any
      has_original:   h.has_original !== false,      // default true if backend omits
      _progress:      carried._progress,
      _phase:         carried._phase,
      _phaseProgress: carried._phaseProgress,
    };
  });
}

function _makeFingerprint(data) {
  return data.map(h => `${h.file}|${h.status}|${h.words}|${h.duration}`).join('\n');
}

async function loadHistory() {
  try {
    const res  = await fetch('/api/history');
    const data = await res.json();
    files = _historyToFiles(data);
    _syncFingerprint = _makeFingerprint(data);
    renderFiles();
  } catch (e) {
    console.error('loadHistory:', e);
    renderFiles([]);
  }
}

// ─── AUTO-SYNC ───────────────────────────────────────────────
// Keeps the page live — checks for history changes every 5s
// and re-renders only if something actually changed.
let _etaTickInterval = null;
function startAutoSync() {
  if (_autoSyncInterval) return;
  _autoSyncInterval = setInterval(_autoSyncTick, 5000);
  // Lighter tick: only update ETAs (no fetch) every 5s so the remaining
  // time feels "live" while a transcription is running.
  if (!_etaTickInterval) {
    _etaTickInterval = setInterval(() => {
      if (document.hidden) return; // saves DOM work while tab is in background
      const hasActive = files.some(f => f.status === 'queued' || f.status === 'processing');
      if (hasActive) {
        _renderQueueSummary();
        // Only re-render rows that have an ETA visible (queued/processing)
        // — cheap because we're iterating a small subset.
        for (const f of files) {
          if (f.status !== 'queued' && f.status !== 'processing') continue;
          const tr = document.querySelector(`#files-tbody tr[data-id="${CSS.escape(f.file)}"] .col-status`);
          if (tr) tr.innerHTML = renderStatus(f.status, f);
        }
      }
    }, 5000);
  }
}

async function _autoSyncTick() {
  if (document.hidden) return; // avoid background fetches while user is away
  // Active polls already reload history on done/error — skip to avoid double render
  if (Object.keys(_activePolls).length > 0) return;
  try {
    const res  = await fetch('/api/history');
    const data = await res.json();
    const fp   = _makeFingerprint(data);
    if (fp === _syncFingerprint) return; // nothing changed
    _syncFingerprint = fp;
    files = _historyToFiles(data);
    renderFiles();
    loadStats();
    loadFolders(); // counts may have changed
    // Restart polling for any active tasks discovered (e.g. uploaded from another tab)
    files.forEach(f => {
      if ((f.status === 'queued' || f.status === 'processing') && f.task_id && !_activePolls[f.task_id]) {
        pollProgressForRow(f.task_id, f.file);
      }
    });
  } catch { /* network blip — ignore */ }
}

async function loadStats() {
  try {
    const res  = await fetch('/api/stats');
    const data = await res.json();
    document.getElementById('stat-count').textContent = data.total ?? '0';
    document.getElementById('stat-hours').textContent = data.duration || '0m';
  } catch { /* ignore */ }
}

// ═══════════════════════════════════════════════════════════════
//  FOCUS TRAP — keeps keyboard navigation inside an open overlay
// ═══════════════════════════════════════════════════════════════
const _FOCUSABLE_SEL = [
  'a[href]', 'button:not([disabled])', 'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])', 'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])', '[contenteditable="true"]'
].join(',');

// Map<overlayEl, {handler, returnFocusEl}>
const _activeTraps = new Map();

function _focusable(root) {
  return Array.from(root.querySelectorAll(_FOCUSABLE_SEL))
    .filter(el => el.offsetParent !== null || el === document.activeElement);
}

function attachFocusTrap(overlayId) {
  const overlay = document.getElementById(overlayId);
  if (!overlay || _activeTraps.has(overlay)) return;
  const returnFocusEl = document.activeElement;
  const handler = (e) => {
    if (e.key !== 'Tab') return;
    const items = _focusable(overlay);
    if (!items.length) { e.preventDefault(); return; }
    const first = items[0], last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && active === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
    // Also recover if focus has somehow escaped the overlay
    else if (!overlay.contains(active)) { e.preventDefault(); first.focus(); }
  };
  overlay.addEventListener('keydown', handler);
  _activeTraps.set(overlay, { handler, returnFocusEl });
}

function detachFocusTrap(overlayId) {
  const overlay = document.getElementById(overlayId);
  if (!overlay) return;
  const entry = _activeTraps.get(overlay);
  if (!entry) return;
  overlay.removeEventListener('keydown', entry.handler);
  _activeTraps.delete(overlay);
  // Restore focus to whatever triggered the open (skip if it's gone or hidden)
  const r = entry.returnFocusEl;
  if (r && document.contains(r) && r.offsetParent !== null) {
    try { r.focus(); } catch {}
  }
}

// ═══════════════════════════════════════════════════════════════
//  IN-SITE DIALOGS (substituem prompt()/confirm() nativos)
//  Um único modal reutilizável, promise-based. Chamadas:
//    await showConfirm({ title, message, confirmText, cancelText, danger })
//    await showPrompt ({ title, message, initialValue, placeholder,
//                        confirmText, validator })
//    await showChoice ({ title, message, choices: [{ value, label,
//                        description?, danger?, icon? (SVG HTML) }] })
// Todas retornam Promise. Confirm -> true|false, Prompt -> string|null,
// Choice -> value|null (null = cancelado).
// ═══════════════════════════════════════════════════════════════
let _dialogResolver = null; // função que resolve a promise pendente

function _dialogClose(result) {
  const overlay = document.getElementById('dialog-overlay');
  if (!overlay) return;
  overlay.classList.remove('open');
  document.body.style.overflow = '';
  detachFocusTrap('dialog-overlay');
  if (_dialogResolver) {
    const fn = _dialogResolver;
    _dialogResolver = null;
    fn(result);
  }
}

function _dialogCancel() {
  // Decide o valor "cancelado" com base no tipo ativo (armazenado em dataset)
  const overlay = document.getElementById('dialog-overlay');
  const type = overlay?.dataset?.type;
  if (type === 'confirm')      _dialogClose(false);
  else                          _dialogClose(null); // prompt ou choice
}

// Ícones por tipo — ajuda a comunicar intenção visual
const _DIALOG_ICONS = {
  info:    `<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>`,
  edit:    `<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>`,
  folder:  `<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>`,
  warning: `<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>`,
  danger:  `<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/>`,
};

function _setDialogIcon(kind, color) {
  const svg = document.getElementById('dialog-icon');
  if (!svg) return;
  svg.innerHTML = _DIALOG_ICONS[kind] || _DIALOG_ICONS.info;
  svg.style.color = color || 'var(--blue)';
}

function _showDialog({ type, title, iconKind, iconColor, bodyBuilder, footBuilder }) {
  return new Promise((resolve) => {
    // Se já existe diálogo aberto, força fechar (cancelar) antes de abrir novo.
    if (_dialogResolver) { const prev = _dialogResolver; _dialogResolver = null; prev(null); }
    _dialogResolver = resolve;

    const overlay = document.getElementById('dialog-overlay');
    overlay.dataset.type = type;
    document.getElementById('dialog-title').textContent = title || '';

    _setDialogIcon(iconKind || 'info', iconColor);

    // Clear body
    const msg = document.getElementById('dialog-message');
    const inp = document.getElementById('dialog-input');
    const err = document.getElementById('dialog-error');
    const ch  = document.getElementById('dialog-choices');
    msg.textContent = '';
    msg.style.display = 'none';
    inp.style.display = 'none'; inp.classList.remove('invalid'); inp.value = '';
    err.style.display = 'none'; err.textContent = '';
    ch.style.display  = 'none'; ch.innerHTML = '';

    bodyBuilder({ msg, inp, err, ch });
    document.getElementById('dialog-foot').innerHTML = '';
    footBuilder(document.getElementById('dialog-foot'));

    overlay.classList.add('open');
    document.body.style.overflow = 'hidden';
    attachFocusTrap('dialog-overlay');

    // Foco: input se houver, senão primeiro botão do footer
    setTimeout(() => {
      if (inp.style.display !== 'none') { inp.focus(); inp.select(); }
      else {
        const firstBtn = document.querySelector('#dialog-foot button, #dialog-choices button');
        if (firstBtn) firstBtn.focus();
      }
    }, 60);
  });
}

function showConfirm({ title = 'Confirmar', message = '', confirmText = 'Confirmar', cancelText = 'Cancelar', danger = false } = {}) {
  return _showDialog({
    type: 'confirm',
    title,
    iconKind: danger ? 'danger' : 'warning',
    iconColor: danger ? 'var(--red)' : 'var(--blue)',
    bodyBuilder: ({ msg }) => {
      msg.textContent = message;
      msg.style.display = 'block';
    },
    footBuilder: (foot) => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:10px;width:100%';
      const ok = document.createElement('button');
      ok.className = danger ? 'btn btn-danger-lg' : 'btn btn-primary-lg';
      ok.style.flex = '1';
      ok.textContent = confirmText;
      ok.onclick = () => _dialogClose(true);
      const cancel = document.createElement('button');
      cancel.className = 'btn btn-secondary-lg';
      cancel.style.flex = '1';
      cancel.textContent = cancelText;
      cancel.onclick = () => _dialogClose(false);
      row.appendChild(cancel);
      row.appendChild(ok);
      foot.appendChild(row);
    }
  });
}

function showPrompt({ title = 'Digite um valor', message = '', initialValue = '', placeholder = '', confirmText = 'Confirmar', cancelText = 'Cancelar', validator = null, iconKind = 'edit' } = {}) {
  return _showDialog({
    type: 'prompt',
    title,
    iconKind,
    bodyBuilder: ({ msg, inp, err }) => {
      if (message) { msg.textContent = message; msg.style.display = 'block'; }
      inp.style.display = 'block';
      inp.value = initialValue;
      inp.placeholder = placeholder || '';
      const doConfirm = () => {
        const val = inp.value;
        if (validator) {
          const errMsg = validator(val);
          if (errMsg) {
            err.textContent = errMsg; err.style.display = 'block';
            inp.classList.add('invalid'); inp.focus();
            return;
          }
        }
        _dialogClose(val);
      };
      inp.onkeydown = (e) => {
        if (e.key === 'Enter') { e.preventDefault(); doConfirm(); }
        else { err.style.display = 'none'; inp.classList.remove('invalid'); }
      };
      // Exponha o handler pro footer usar
      inp.dataset._bound = '1';
      inp._confirm = doConfirm;
    },
    footBuilder: (foot) => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:10px;width:100%';
      const ok = document.createElement('button');
      ok.className = 'btn btn-primary-lg'; ok.style.flex = '1';
      ok.textContent = confirmText;
      ok.onclick = () => { document.getElementById('dialog-input')._confirm(); };
      const cancel = document.createElement('button');
      cancel.className = 'btn btn-secondary-lg'; cancel.style.flex = '1';
      cancel.textContent = cancelText;
      cancel.onclick = () => _dialogClose(null);
      row.appendChild(cancel);
      row.appendChild(ok);
      foot.appendChild(row);
    }
  });
}

// Multi-select checkbox dialog. `options` is [{ value, label, description?, defaultChecked? }].
// Resolves with an array of chosen values, or null if cancelled.
function showMultiChoice({ title = 'Selecionar', message = '', options = [], confirmText = 'Confirmar', cancelText = 'Cancelar', iconKind = 'folder' } = {}) {
  return _showDialog({
    type: 'multichoice',
    title,
    iconKind,
    bodyBuilder: ({ msg, ch }) => {
      if (message) { msg.textContent = message; msg.style.display = 'block'; }
      ch.style.display = 'flex';
      ch.innerHTML = '';
      options.forEach((o, i) => {
        const lbl = document.createElement('label');
        lbl.className = 'dialog-choice';
        lbl.style.cursor = 'pointer';
        const checked = o.defaultChecked !== false ? 'checked' : '';
        lbl.innerHTML = `
          <input type="checkbox" class="checkbox" data-mc-value="${esc(String(o.value))}" ${checked}
                 style="width:18px;height:18px;flex-shrink:0" />
          <div class="dialog-choice-body">
            <div class="dialog-choice-label">${esc(o.label)}</div>
            ${o.description ? `<div class="dialog-choice-sub">${esc(o.description)}</div>` : ''}
          </div>`;
        ch.appendChild(lbl);
      });
    },
    footBuilder: (foot) => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:10px;width:100%';
      const ok = document.createElement('button');
      ok.className = 'btn btn-primary-lg'; ok.style.flex = '1';
      ok.textContent = confirmText;
      ok.onclick = () => {
        const boxes = document.querySelectorAll('#dialog-choices input[type="checkbox"][data-mc-value]');
        const values = Array.from(boxes).filter(b => b.checked).map(b => b.dataset.mcValue);
        _dialogClose(values);
      };
      const cancel = document.createElement('button');
      cancel.className = 'btn btn-secondary-lg'; cancel.style.flex = '1';
      cancel.textContent = cancelText;
      cancel.onclick = () => _dialogClose(null);
      row.appendChild(cancel);
      row.appendChild(ok);
      foot.appendChild(row);
    }
  });
}

function showChoice({ title = 'Selecionar', message = '', choices = [], cancelText = 'Cancelar', iconKind = 'folder' } = {}) {
  return _showDialog({
    type: 'choice',
    title,
    iconKind,
    bodyBuilder: ({ msg, ch }) => {
      if (message) { msg.textContent = message; msg.style.display = 'block'; }
      ch.style.display = 'flex';
      ch.innerHTML = '';
      for (const c of choices) {
        const btn = document.createElement('button');
        btn.className = 'dialog-choice' + (c.danger ? ' danger' : '');
        btn.innerHTML = `
          <div class="dialog-choice-icon">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${c.icon || _DIALOG_ICONS.folder}</svg>
          </div>
          <div class="dialog-choice-body">
            <div class="dialog-choice-label">${esc(c.label)}</div>
            ${c.description ? `<div class="dialog-choice-sub">${esc(c.description)}</div>` : ''}
          </div>`;
        btn.onclick = () => _dialogClose(c.value);
        ch.appendChild(btn);
      }
    },
    footBuilder: (foot) => {
      const cancel = document.createElement('button');
      cancel.className = 'btn btn-secondary-lg'; cancel.style.width = '100%';
      cancel.textContent = cancelText;
      cancel.onclick = () => _dialogClose(null);
      foot.appendChild(cancel);
    }
  });
}

// ═══════════════════════════════════════════════════════════════
//  FOLDERS — tree, CRUD, sidebar
// ═══════════════════════════════════════════════════════════════
// _folders: flat list of {path, count} returned by backend.
// _expandedFolders: Set of paths currently expanded in the tree UI.
let _folders = [];
const _expandedFolders = new Set();

async function loadFolders() {
  try {
    const res = await fetch('/api/folders');
    if (!res.ok) { _folders = []; renderFolderTree(); return; }
    _folders = await res.json();
  } catch { _folders = []; }
  renderFolderTree();
}

// Build a nested tree from the flat list of paths + counts.
// Also injects items with folder='' into the root "(sem pasta)" bucket for display.
function _buildFolderTree() {
  const root = { path: '', name: '(raiz)', count: 0, children: {} };
  for (const f of _folders) {
    const segments = f.path.split('/');
    let node = root;
    let curPath = '';
    for (const seg of segments) {
      curPath = curPath ? `${curPath}/${seg}` : seg;
      if (!node.children[seg]) {
        node.children[seg] = { path: curPath, name: seg, count: 0, children: {} };
      }
      node = node.children[seg];
    }
    node.count = f.count;
  }
  return root;
}

function renderFolderTree() {
  const container = document.getElementById('folder-tree');
  if (!container) return;
  const tree = _buildFolderTree();

  // In-memory counts for the special "virtual" buckets
  const allCount  = files.length;
  const rootCount = files.filter(f => !(f.folder || '')).length;

  const html = [];
  html.push(`<ul>`);
  html.push(_renderFolderNodeHTML({
    path: '__all__', name: 'Todas', count: allCount, isVirtual: true
  }));
  html.push(_renderFolderNodeHTML({
    path: '__root__', name: '(sem pasta)', count: rootCount, isVirtual: true
  }));
  // Top-level folders from the tree
  for (const name of Object.keys(tree.children).sort()) {
    html.push(_renderFolderSubtreeHTML(tree.children[name]));
  }
  html.push(`</ul>`);
  container.innerHTML = html.join('');
}

function _renderFolderNodeHTML(node, hasChildren = false) {
  const filterKey = node.isVirtual ? (node.path === '__all__' ? 'all' : '__root__') : node.path;
  const active = _view.folderFilter === filterKey;
  const expanded = hasChildren && _expandedFolders.has(node.path);
  const twist = hasChildren
    ? `<span class="twist" onclick="event.stopPropagation(); toggleFolderExpand('${jsAttr(node.path)}')" aria-hidden="true">▸</span>`
    : `<span class="twist leaf" aria-hidden="true">▸</span>`;
  const icon = node.isVirtual && node.path === '__all__'
    ? `<svg class="ficon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>`
    : `<svg class="ficon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`;
  const menuBtn = node.isVirtual ? '' :
    `<button type="button" class="folder-menu-btn" aria-label="Ações da pasta" onclick="event.stopPropagation(); folderMenu('${jsAttr(node.path)}', event)">
       <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="5" r="1" fill="currentColor"/><circle cx="12" cy="12" r="1" fill="currentColor"/><circle cx="12" cy="19" r="1" fill="currentColor"/></svg>
     </button>`;
  return `
    <li>
      <div class="folder-node ${active ? 'active' : ''} ${expanded ? 'expanded' : ''}"
           role="button" tabindex="0"
           onclick="selectFolder('${jsAttr(filterKey)}')"
           onkeydown="if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectFolder('${jsAttr(filterKey)}'); }">
        ${twist}
        ${icon}
        <span class="folder-label" title="${esc(node.name)}">${esc(node.name)}</span>
        <span class="folder-count">${node.count}</span>
        ${menuBtn}
      </div>
    </li>`;
}

function _renderFolderSubtreeHTML(node) {
  const hasChildren = Object.keys(node.children).length > 0;
  const nodeHTML = _renderFolderNodeHTML(node, hasChildren);
  if (!hasChildren) return nodeHTML;
  const expanded = _expandedFolders.has(node.path);
  const childrenHTML = `<ul class="folder-child-nodes" style="display:${expanded ? 'block' : 'none'}">${Object.keys(node.children).sort().map(n => _renderFolderSubtreeHTML(node.children[n])).join('')}</ul>`;
  // Inject children UL after the node's LI (still inside the parent UL)
  // We wrap nodeHTML+childrenHTML into a single <li> structure:
  return nodeHTML.replace('</li>', childrenHTML + '</li>');
}

function selectFolder(pathOrKey) {
  _view.folderFilter = pathOrKey;
  // Auto-expand ancestors when selecting a nested folder
  if (pathOrKey !== 'all' && pathOrKey !== '__root__' && pathOrKey) {
    const segments = pathOrKey.split('/');
    for (let i = 0; i < segments.length - 1; i++) {
      _expandedFolders.add(segments.slice(0, i + 1).join('/'));
    }
  }
  closeSidebar();
  renderFolderTree();
  renderFiles();
}

function toggleFolderExpand(path) {
  if (_expandedFolders.has(path)) _expandedFolders.delete(path);
  else _expandedFolders.add(path);
  renderFolderTree();
}

// Sidebar mobile drawer
function openSidebar() {
  document.getElementById('folder-sidebar').classList.add('open');
  document.getElementById('sidebar-backdrop').classList.add('open');
}
function closeSidebar() {
  document.getElementById('folder-sidebar').classList.remove('open');
  document.getElementById('sidebar-backdrop').classList.remove('open');
}

async function folderMenu(path, ev) {
  const action = await showChoice({
    title: `Pasta "${path}"`,
    message: 'O que você deseja fazer?',
    iconKind: 'folder',
    choices: [
      { value: 'subfolder', label: 'Criar subpasta', description: 'Adiciona uma nova pasta dentro desta',
        icon: `<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/>` },
      { value: 'rename',    label: 'Renomear pasta',  description: 'Atualiza o nome; descendentes e itens são ajustados',
        icon: _DIALOG_ICONS.edit },
      { value: 'delete',    label: 'Excluir pasta',   description: 'Com opção de mover ou apagar o conteúdo',
        danger: true, icon: _DIALOG_ICONS.danger },
    ]
  });
  if (action === 'subfolder') await promptCreateFolder(path);
  else if (action === 'rename') await promptRenameFolder(path);
  else if (action === 'delete') await promptDeleteFolder(path);
}

async function promptCreateFolder(parentPath) {
  const name = await showPrompt({
    title: parentPath ? 'Nova subpasta' : 'Nova pasta',
    message: parentPath
      ? `A subpasta será criada dentro de "${parentPath}".`
      : 'Digite o nome da nova pasta.',
    placeholder: 'Ex: Projetos 2026',
    confirmText: 'Criar',
    iconKind: 'folder',
    validator: (val) => {
      const v = (val || '').trim();
      if (!v) return 'O nome não pode ficar em branco.';
      if (v.length > 60) return 'Máximo de 60 caracteres por segmento.';
      if (/[\/\\\x00]/.test(v)) return 'O nome não pode conter /, \\ ou caracteres de controle.';
      if (v === '.' || v === '..') return 'Nome inválido.';
      return null;
    }
  });
  if (name === null) return;
  const clean = name.trim();
  if (!clean) return;
  const fullPath = parentPath ? `${parentPath}/${clean}` : clean;
  try {
    const fd = new FormData();
    fd.append('path', fullPath);
    const res = await fetch('/api/folders/create', { method: 'POST', body: fd });
    if (!res.ok) {
      let msg = 'Erro ao criar pasta.';
      try { const e = await res.json(); if (e.detail) msg = e.detail; } catch {}
      showToast(msg, 'error');
      return;
    }
    // Auto-expand ancestors
    if (parentPath) {
      const segs = parentPath.split('/');
      for (let i = 0; i < segs.length; i++) _expandedFolders.add(segs.slice(0, i + 1).join('/'));
    }
    await loadFolders();
    showToast(`Pasta "${fullPath}" criada.`, 'success');
  } catch {
    showToast('Erro ao criar pasta.', 'error');
  }
}

async function promptRenameFolder(oldPath) {
  const segments = oldPath.split('/');
  const oldName = segments[segments.length - 1];
  const newName = await showPrompt({
    title: 'Renomear pasta',
    message: `Pasta atual: "${oldPath}"\nDescendentes e itens dentro serão atualizados automaticamente.`,
    initialValue: oldName,
    confirmText: 'Renomear',
    iconKind: 'edit',
    validator: (val) => {
      const v = (val || '').trim();
      if (!v) return 'O nome não pode ficar em branco.';
      if (v.length > 60) return 'Máximo de 60 caracteres.';
      if (/[\/\\\x00]/.test(v)) return 'O nome não pode conter /, \\ ou caracteres de controle.';
      if (v === '.' || v === '..') return 'Nome inválido.';
      return null;
    }
  });
  if (newName === null) return;
  const clean = newName.trim();
  if (!clean || clean === oldName) return;
  const parent = segments.slice(0, -1).join('/');
  const newPath = parent ? `${parent}/${clean}` : clean;
  try {
    const fd = new FormData();
    fd.append('old_path', oldPath);
    fd.append('new_path', newPath);
    const res = await fetch('/api/folders/rename', { method: 'POST', body: fd });
    if (!res.ok) {
      let msg = 'Erro ao renomear pasta.';
      try { const e = await res.json(); if (e.detail) msg = e.detail; } catch {}
      showToast(msg, 'error');
      return;
    }
    // If we had this folder selected, update filter to the new path
    if (_view.folderFilter === oldPath) _view.folderFilter = newPath;
    else if (_view.folderFilter.startsWith(oldPath + '/')) {
      _view.folderFilter = newPath + _view.folderFilter.slice(oldPath.length);
    }
    await Promise.all([loadFolders(), loadHistory()]);
    showToast(`Renomeada para "${newPath}".`, 'success');
  } catch {
    showToast('Erro ao renomear pasta.', 'error');
  }
}

async function promptDeleteFolder(path) {
  const itemsInside = files.filter(f => (f.folder || '') === path || (f.folder || '').startsWith(path + '/'));
  let cascade = 'move';
  if (itemsInside.length > 0) {
    const parent = path.split('/').slice(0, -1).join('/');
    const parentLabel = parent || '(raiz)';
    const choice = await showChoice({
      title: `Excluir pasta "${path}"`,
      message: `A pasta contém ${itemsInside.length} item(ns). O que fazer com o conteúdo?`,
      iconKind: 'warning',
      choices: [
        { value: 'move',
          label: `Mover para ${parentLabel}`,
          description: 'Os itens são preservados e vão para a pasta-pai.',
          icon: `<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><polyline points="9 12 12 9 15 12"/><line x1="12" y1="9" x2="12" y2="16"/>` },
        { value: 'delete',
          label: 'Excluir todos os itens',
          description: 'Apaga os arquivos de transcrição do disco. Ação irreversível.',
          danger: true, icon: _DIALOG_ICONS.danger },
      ]
    });
    if (choice === null) return;
    cascade = choice;
  } else {
    const ok = await showConfirm({
      title: 'Excluir pasta vazia',
      message: `Excluir a pasta "${path}"?`,
      confirmText: 'Excluir',
      danger: true
    });
    if (!ok) return;
  }
  try {
    const fd = new FormData();
    fd.append('path', path);
    fd.append('cascade', cascade);
    const res = await fetch('/api/folders/delete', { method: 'POST', body: fd });
    if (!res.ok) {
      let msg = 'Erro ao excluir pasta.';
      try { const e = await res.json(); if (e.detail) msg = e.detail; } catch {}
      showToast(msg, 'error');
      return;
    }
    // If we had this folder (or descendant) selected, reset filter to parent/all
    if (_view.folderFilter === path || _view.folderFilter.startsWith(path + '/')) {
      const parent = path.split('/').slice(0, -1).join('/');
      _view.folderFilter = parent || 'all';
    }
    await Promise.all([loadFolders(), loadHistory()]);
    showToast('Pasta excluída.', 'success');
  } catch {
    showToast('Erro ao excluir pasta.', 'error');
  }
}

// ───── Folder picker modal (usado pelo "Mover para pasta") ─────
// Target can be a single fileId or an array (bulk move).
let _pickerTargetFileIds = [];
let _pickerSelected = ''; // '' = raiz

function openFolderPicker(fileIdOrIds) {
  const ids = Array.isArray(fileIdOrIds) ? fileIdOrIds : [fileIdOrIds];
  _pickerTargetFileIds = ids;
  const first = ids.length === 1 ? files.find(x => x.id === ids[0]) : null;
  // Pre-select shared folder if all selected items live in the same folder
  if (ids.length > 1) {
    const folders = ids.map(id => (files.find(x => x.id === id) || {}).folder || '');
    const allSame = folders.every(f => f === folders[0]);
    _pickerSelected = allSame ? folders[0] : '';
  } else {
    _pickerSelected = first ? (first.folder || '') : '';
  }
  document.getElementById('folder-picker-subtitle').textContent =
    ids.length > 1
      ? `Mover ${ids.length} arquivo(s) para qual pasta?`
      : (first ? `Mover "${first.name}" para qual pasta?` : 'Selecione a pasta de destino:');
  document.getElementById('folder-picker-new').value = '';
  _renderFolderPickerList();
  document.getElementById('folder-picker-overlay').classList.add('open');
  document.body.style.overflow = 'hidden';
  attachFocusTrap('folder-picker-overlay');
  setTimeout(() => document.querySelector('#folder-picker-overlay .modal-close')?.focus(), 50);
}

function closeFolderPicker() {
  document.getElementById('folder-picker-overlay').classList.remove('open');
  document.body.style.overflow = '';
  detachFocusTrap('folder-picker-overlay');
  _pickerTargetFileIds = [];
}

// ═══════════════════════════════════════════════════════════════
//  SETTINGS — concurrency for download + transcribe
// ═══════════════════════════════════════════════════════════════
// Set a <select>'s value, injecting the option first if the saved value isn't
// one of the presets (backend allows any 1–16) — avoids a blank dropdown.
function _setConcurrencyValue(id, val) {
  const sel = document.getElementById(id);
  if (!sel) return;
  const v = String(val);
  if (![...sel.options].some(o => o.value === v)) {
    const opt = document.createElement('option');
    opt.value = v; opt.textContent = v;
    sel.appendChild(opt);
  }
  sel.value = v;
}

async function openSettings() {
  // Pull current values from backend so the user always sees the live state.
  // The selects already carry sane defaults (3 / 1) via `selected`, so even if
  // this fetch fails the dropdowns are never blank.
  try {
    const r = await fetch('/api/settings');
    if (r.ok) {
      const s = await r.json();
      _settingsCache = s;  // keep ETA wall-clock math in sync with truth
      _setConcurrencyValue('set-download-concurrent',   s.download_concurrent   ?? 3);
      _setConcurrencyValue('set-transcribe-concurrent', s.transcribe_concurrent ?? 1);
    }
  } catch { /* defaults already selected in DOM */ }
  document.getElementById('settings-overlay').classList.add('open');
  document.body.style.overflow = 'hidden';
  attachFocusTrap('settings-overlay');
  setTimeout(() => document.getElementById('set-download-concurrent')?.focus(), 50);
}

function closeSettings() {
  document.getElementById('settings-overlay').classList.remove('open');
  document.body.style.overflow = '';
  detachFocusTrap('settings-overlay');
}

async function saveSettings() {
  const dl = parseInt(document.getElementById('set-download-concurrent').value, 10);
  const tr = parseInt(document.getElementById('set-transcribe-concurrent').value, 10);
  if (!(dl >= 1 && dl <= 16) || !(tr >= 1 && tr <= 16)) {
    showToast('Valores devem estar entre 1 e 16.', 'error');
    return;
  }
  const btn = document.getElementById('settings-save-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Salvando…'; }
  try {
    const fd = new FormData();
    fd.append('download_concurrent',   String(dl));
    fd.append('transcribe_concurrent', String(tr));
    const r = await fetch('/api/settings', { method: 'POST', body: fd });
    if (!r.ok) { showToast('Não foi possível salvar.', 'error'); return; }
    _settingsCache = { download_concurrent: dl, transcribe_concurrent: tr };  // refresh local cache
    showToast('Configurações salvas.', 'success');
    closeSettings();
    _renderQueueSummary(); // ETA total agora reflete o novo paralelismo
  } catch {
    showToast('Erro de rede ao salvar.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Salvar'; }
  }
}

function _renderFolderPickerList() {
  const list = document.getElementById('folder-picker-list');
  if (!list) return;
  const tree = _buildFolderTree();
  const html = ['<ul>'];
  html.push(_renderPickerItem({ path: '', name: '(raiz)' }));
  for (const name of Object.keys(tree.children).sort()) {
    html.push(_renderPickerSubtree(tree.children[name]));
  }
  html.push('</ul>');
  list.innerHTML = html.join('');
}

function _renderPickerItem(node) {
  const selected = _pickerSelected === node.path;
  return `<li>
    <div class="folder-picker-item ${selected ? 'selected' : ''}"
         onclick="_pickerSelect('${jsAttr(node.path)}')">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        ${node.path === '' ? '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>' : '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'}
      </svg>
      ${esc(node.name)}
    </div>
  </li>`;
}

function _renderPickerSubtree(node) {
  let html = _renderPickerItem(node);
  const names = Object.keys(node.children).sort();
  if (names.length) {
    html = html.replace('</li>',
      `<ul>${names.map(n => _renderPickerSubtree(node.children[n])).join('')}</ul></li>`);
  }
  return html;
}

function _pickerSelect(path) {
  _pickerSelected = path;
  _renderFolderPickerList();
}

async function folderPickerCreateNew() {
  const inp = document.getElementById('folder-picker-new');
  const raw = (inp.value || '').trim();
  if (!raw) return;
  try {
    const fd = new FormData();
    fd.append('path', raw);
    const res = await fetch('/api/folders/create', { method: 'POST', body: fd });
    if (!res.ok) {
      let msg = 'Erro ao criar pasta.';
      try { const e = await res.json(); if (e.detail) msg = e.detail; } catch {}
      showToast(msg, 'error');
      return;
    }
    inp.value = '';
    _pickerSelected = raw;
    await loadFolders();
    _renderFolderPickerList();
  } catch {
    showToast('Erro ao criar pasta.', 'error');
  }
}

async function folderPickerConfirm() {
  if (!_pickerTargetFileIds.length) return;
  const target = _pickerSelected;
  const targets = _pickerTargetFileIds
    .map(id => files.find(x => x.id === id))
    .filter(Boolean);
  if (!targets.length) { closeFolderPicker(); return; }

  let ok = 0, failed = 0;
  for (const f of targets) {
    try {
      const fd = new FormData();
      fd.append('filename', f.file);
      fd.append('folder', target);
      const res = await fetch('/api/move-to-folder', { method: 'POST', body: fd });
      if (!res.ok) { failed++; continue; }
      f.folder = target;
      ok++;
    } catch { failed++; }
  }
  closeFolderPicker();
  await loadFolders();
  renderFiles();
  syncBulkBar();
  if (ok && !failed) {
    showToast(
      targets.length > 1
        ? `${ok} arquivo(s) movidos${target ? ` para "${target}"` : ' para a raiz'}.`
        : (target ? `Movido para "${target}".` : 'Removido da pasta.'),
      'success');
  } else if (ok && failed) {
    showToast(`${ok} movido(s), ${failed} falhou(ram).`, 'error');
  } else {
    showToast('Erro ao mover.', 'error');
  }
}

function moveSelectedToFolder() {
  const ids = selectedVisibleIds();
  if (!ids.length) {
    showToast('Selecione ao menos um arquivo.', 'error');
    return;
  }
  openFolderPicker(ids);
}

// ═══════════════════════════════════════════════════════════════
//  VIEW CONTROLS (sort / filter / folder / queue summary)
// ═══════════════════════════════════════════════════════════════
function setStatusFilter(status) {
  _view.statusFilter = status;
  document.querySelectorAll('.chip[data-status]').forEach(el => {
    el.setAttribute('aria-pressed', el.dataset.status === status);
  });
  renderFiles();
}

function setFolderFilter(folder) {
  _view.folderFilter = folder || 'all';
  renderFiles();
}

function setSort(key) {
  if (_view.sortKey === key) {
    _view.sortDir = _view.sortDir === 'asc' ? 'desc' : 'asc';
  } else {
    _view.sortKey = key;
    // Sensible default direction per column
    _view.sortDir = (key === 'date') ? 'desc' : 'asc';
  }
  renderFiles();
}

function _renderSortIndicators() {
  document.querySelectorAll('.files-table th.sortable').forEach(th => {
    const active = th.dataset.sort === _view.sortKey;
    const arrow = th.querySelector('.sort-arrow');
    if (!active) {
      th.removeAttribute('aria-sort');
      if (arrow) arrow.textContent = '↕';
    } else {
      th.setAttribute('aria-sort', _view.sortDir === 'asc' ? 'ascending' : 'descending');
      if (arrow) arrow.textContent = _view.sortDir === 'asc' ? '↑' : '↓';
    }
  });
}

function _renderStatusFilterCounts() {
  const counts = { all: files.length, queued:0, processing:0, done:0, error:0 };
  for (const f of files) counts[f.status] = (counts[f.status] || 0) + 1;
  for (const k of Object.keys(counts)) {
    const el = document.getElementById('chip-count-' + k);
    if (el) el.textContent = counts[k];
  }
}

// Sync the mobile "current folder" label on the sidebar-toggle button
function _syncFolderButtonLabel() {
  const el = document.getElementById('current-folder-label');
  if (!el) return;
  const v = _view.folderFilter;
  el.textContent = v === 'all' ? 'Pastas'
                  : v === '__root__' ? 'Sem pasta'
                  : v.split('/').slice(-1)[0];
}

function _renderQueueSummary() {
  const box  = document.getElementById('queue-summary');
  const text = document.getElementById('queue-summary-text');

  // Decompõe pendentes em: já-em-transcrição (ETA real) + na-fila (estimativa).
  // Soma "em série" ÷ concorrência permitida (default 1) — é assim que o lote
  // realmente vai rolar, dado o _transcribe_sem do backend.
  const pending     = files.filter(f => f.status === 'queued' || f.status === 'processing');
  const inProgress  = pending.filter(f => f.status === 'processing');
  const queued      = pending.filter(f => f.status === 'queued');
  const count       = pending.length;

  const now = Date.now() / 1000;
  let inProgressSecs = 0;
  for (const f of inProgress) {
    const est = estimateRemainingSecs(f, now);
    if (est != null) inProgressSecs += est;
  }
  let queuedSecs = 0;
  let queuedUnknown = 0;
  for (const f of queued) {
    const est = estimateRemainingSecs(f, now);
    if (est == null) queuedUnknown++;
    else queuedSecs += est;
  }
  // Concorrência de transcrição (1 por padrão, configurável). Itens em paralelo
  // levam menos wall-clock total — total ≈ tempo_em_curso + soma_fila / N.
  const concurrency = Math.max(1, _settingsCache?.transcribe_concurrent || 1);
  const queueWallClock = queuedSecs / concurrency;
  const totalSecs = inProgressSecs + queueWallClock;

  // Stat card no topo
  const statVal = document.getElementById('stat-queue');
  const statSub = document.getElementById('stat-queue-sub');
  if (statVal && statSub) {
    statVal.textContent = count;
    if (count === 0) statSub.textContent = 'nenhuma transcrição pendente';
    else if (totalSecs > 0) statSub.textContent = `~${fmtSecs(totalSecs)} restantes`;
    else statSub.textContent = 'aguardando ou processando';
  }

  if (!box || !text) return;
  if (count === 0) { box.classList.remove('show'); return; }
  box.classList.add('show');

  // UI breakdown: ETA explícita do que está rolando + contagem na fila + total
  const parts = [];
  if (inProgress.length) {
    parts.push(inProgressSecs > 0
      ? `${inProgress.length} em transcrição (~<strong>${fmtSecs(inProgressSecs)}</strong> p/ terminar)`
      : `${inProgress.length} em transcrição`);
  }
  if (queued.length) {
    let s = `${queued.length} na fila`;
    if (queuedSecs > 0) s += ` · ~<strong>${fmtSecs(queueWallClock)}</strong>`;
    if (queuedUnknown > 0) s += ` (${queuedUnknown} sem estimativa)`;
    parts.push(s);
  }
  let msg = parts.join(' · ');
  if (totalSecs > 0 && inProgress.length && queued.length) {
    msg += ` · <strong>total ~${fmtSecs(totalSecs)}</strong>`;
  }
  text.innerHTML = msg;
}

// Cache local das settings — usado pra calcular wall-clock com concorrência.
// Atualizado em loadSettings/openSettings/saveSettings; default conservador (1).
let _settingsCache = { download_concurrent: 1, transcribe_concurrent: 1 };
(async () => {
  try {
    const r = await fetch('/api/settings');
    if (r.ok) _settingsCache = await r.json();
  } catch { /* defaults serve */ }
})();

function promptMoveToFolder(id) {
  closeAllDDs();
  openFolderPicker(id);
}

// ═══════════════════════════════════════════════════════════════
//  RENDER
// ═══════════════════════════════════════════════════════════════
// Escapes for safe insertion into HTML attribute values quoted with double OR single quotes.
// Critical: filenames may contain ' or " — without escaping these, onclick="..(' + name + ')..."
// breaks the attribute and allows JS injection.
function esc(s) {
  return String(s)
    .replace(/&/g,  '&amp;')
    .replace(/</g,  '&lt;')
    .replace(/>/g,  '&gt;')
    .replace(/"/g,  '&quot;')
    .replace(/'/g,  '&#39;')
    .replace(/`/g,  '&#96;');
}

// Escapes a string for safe insertion as a JS string LITERAL inside an HTML attribute
// (e.g. onclick="foo('${jsAttr(name)}')"). Combines JS string escaping with HTML escaping.
function jsAttr(s) {
  return esc(String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'"));
}

// Status types emitidos pelo backend: queued | processing | done | error.
// (Os antigos 'failed' e 'cancelled' eram código morto — removidos.)
const STATUS_MAP = {
  done:       { label:'Concluído',   cls:'done'       },
  processing: { label:'Processando', cls:'processing'  },
  queued:     { label:'Aguardando',  cls:'queued'      },
  error:      { label:'Erro',        cls:'error'       },
  cancelled:  { label:'Cancelado',   cls:'queued'      },
};

// ═══════════════════════════════════════════════════════════════
//  VIEW STATE (filtros + ordenação)
// ═══════════════════════════════════════════════════════════════
// Pipeline: files -> search -> statusFilter -> folderFilter -> sort -> render
const _view = {
  search:       '',
  statusFilter: 'all',      // 'all' | 'queued' | 'processing' | 'done' | 'error'
  folderFilter: 'all',      // 'all' | '__root__' | '<folder name>'
  sortKey:      'date',     // 'name' | 'date' | 'mode' | 'status'
  sortDir:      'desc',     // 'asc' | 'desc'
};

// Canonical ordering for the "status" column sort.
const _STATUS_ORDER = { processing: 0, queued: 1, done: 2, error: 3 };

function _cmp(a, b) { return a < b ? -1 : a > b ? 1 : 0; }

function _sortValue(f, key) {
  switch (key) {
    case 'name':   return (f.name || '').toLowerCase();
    case 'mode':   return (f.mode || '').toLowerCase();
    case 'status': return _STATUS_ORDER[f.status] ?? 99;
    // Date: queued_at is epoch seconds; falls back to 0 for legacy entries.
    // Entries are already in "newest first" insertion order on load, so we
    // use array index as a tiebreaker stored on the object (see applyPipeline).
    case 'date':
    default:       return f.queued_at || 0;
  }
}

function applyPipeline(allFiles) {
  // Stamp each file with its original array index so we have a stable
  // tiebreaker for date sorting of legacy entries without queued_at.
  const stamped = allFiles.map((f, i) => ({ ...f, _order: i }));
  const q = _view.search.trim().toLowerCase();
  let arr = stamped;
  if (q) arr = arr.filter(f => (f.name || '').toLowerCase().includes(q));
  if (_view.statusFilter !== 'all') arr = arr.filter(f => f.status === _view.statusFilter);
  if (_view.folderFilter !== 'all') {
    if (_view.folderFilter === '__root__') {
      arr = arr.filter(f => !(f.folder || ''));
    } else {
      // Include items in this folder AND in any descendant folder
      const wanted = _view.folderFilter;
      const prefix = wanted + '/';
      arr = arr.filter(f => {
        const fp = f.folder || '';
        return fp === wanted || fp.startsWith(prefix);
      });
    }
  }
  const key = _view.sortKey;
  const dir = _view.sortDir === 'asc' ? 1 : -1;
  arr.sort((a, b) => {
    const av = _sortValue(a, key);
    const bv = _sortValue(b, key);
    const c = _cmp(av, bv);
    // Tiebreaker: original order (so sort is stable + legacy dates keep their order)
    if (c !== 0) return c * dir;
    return (a._order - b._order) * (key === 'date' ? dir : 1);
  });
  return arr;
}

function _getVisibleFiles() { return applyPipeline(files); }

// ═══════════════════════════════════════════════════════════════
//  TIME ESTIMATION
// ═══════════════════════════════════════════════════════════════
// Per-model rate: seconds of wall-clock processing per second of audio.
// Computed from completed history entries. Returns null if no data for that model.
function _ratePerAudioSec(model) {
  let totalProc = 0, totalAudio = 0;
  for (const f of files) {
    if (f.status !== 'done' || !f.processing_secs || !f.dur_secs) continue;
    if (f.mode !== model) continue;
    totalProc  += f.processing_secs;
    totalAudio += f.dur_secs;
  }
  if (totalAudio <= 0) return null;
  return totalProc / totalAudio;
}

// Fallback: median total processing time for that model (when we don't know
// the audio duration of a queued entry yet). Median, not mean — outliers
// (1 video gigante de 2h) não envenenam o estimate.
function _avgTotalProcessingSecs(model) {
  const samples = files
    .filter(f => f.status === 'done' && f.processing_secs && f.mode === model)
    .map(f => f.processing_secs)
    .sort((a, b) => a - b);
  if (!samples.length) return null;
  // Prefere amostras recentes (últimas 50) — máquina/rede mudam ao longo do tempo
  const recent = samples.length > 50 ? samples.slice(-50) : samples;
  const mid = Math.floor(recent.length / 2);
  return recent.length % 2 ? recent[mid] : (recent[mid - 1] + recent[mid]) / 2;
}

// Estimate remaining seconds for a single entry. Returns null if unknown.
function estimateRemainingSecs(f, nowTs) {
  nowTs = nowTs || (Date.now() / 1000);
  if (f.status === 'done' || f.status === 'error') return 0;

  // PREFERRED: derive remaining time from the percentage the user is actually
  // looking at — phase_progress (0–100 of the current phase). The backend
  // sets `started_at` when the transcribe phase starts, so for that phase
  // `elapsed` is genuinely the transcription elapsed time:
  //   remaining ≈ elapsed * (100 - p) / p
  // This makes ETA self-correct to the real machine speed and match the bar.
  if (f.status === 'processing' && f.started_at && f._phase === 'transcribe'
      && typeof f._phaseProgress === 'number' && f._phaseProgress >= 3) {
    const elapsed = nowTs - f.started_at;
    const p = Math.min(99.5, f._phaseProgress);
    if (elapsed > 0 && p > 0) return Math.max(0, elapsed * (100 - p) / p);
  }
  // Same idea, fallback when only overall progress is known.
  if (f.status === 'processing' && f.started_at && typeof f._progress === 'number' && f._progress >= 3) {
    const elapsed = nowTs - f.started_at;
    const p = Math.min(99.5, f._progress);
    if (elapsed > 0 && p > 0) return Math.max(0, elapsed * (100 - p) / p);
  }

  // FALLBACK (no usable live progress yet): heuristic from history.
  // duration × rate when known, else the average total processing time.
  const rate = _ratePerAudioSec(f.mode);
  let total = null;
  if (f.dur_secs && rate) total = f.dur_secs * rate;
  else                    total = _avgTotalProcessingSecs(f.mode);
  if (total == null) return null;
  if (f.status === 'processing' && f.started_at) {
    const elapsed = nowTs - f.started_at;
    return Math.max(0, total - elapsed);
  }
  return total;
}

function estimateTotalRemaining() {
  const pending = files.filter(f => f.status === 'queued' || f.status === 'processing');
  let total = 0, unknown = 0;
  const now = Date.now() / 1000;
  for (const f of pending) {
    const est = estimateRemainingSecs(f, now);
    if (est == null) unknown++;
    else total += est;
  }
  return { count: pending.length, totalSecs: total, unknown };
}

function fmtSecs(s) {
  if (s == null || !isFinite(s)) return '—';
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? `${m}m ${r}s` : `${m}m`;
  const h = Math.floor(m / 60), mm = m % 60;
  return mm ? `${h}h ${mm}m` : `${h}h`;
}

// Phase labels — show "Baixando 35%" vs "Transcrevendo 67%" so the user
// can tell download from transcription at a glance.
const _PHASE_LABEL = {
  download:   'Baixando',
  transcribe: 'Transcrevendo',
  saving:     'Salvando',
};

function renderStatus(status, f) {
  const s = STATUS_MAP[status] || STATUS_MAP.queued;
  const extra = status === 'error' ? ' title="Clique para ver o erro"' : '';

  // Label + live percentage — phase-aware when processing.
  let label = s.label;
  let pctLabel = '';
  if (f && status === 'processing') {
    const phaseLabel = _PHASE_LABEL[f._phase];
    if (phaseLabel) label = phaseLabel;
    // Prefer phase_progress (the 0–100 of the CURRENT phase — more intuitive).
    // Fall back to overall progress if phase_progress isn't reported yet.
    const pct = (typeof f._phaseProgress === 'number') ? f._phaseProgress
              : (typeof f._progress === 'number')      ? f._progress
              : null;
    if (pct != null) pctLabel = ` <small style="opacity:.7">${Math.floor(pct)}%</small>`;
  }

  // ETA só faz sentido para itens REALMENTE em processamento — a estimativa
  // depende de elapsed_in_phase, e queued não tem elapsed. Mostrar o mesmo "27s"
  // para 100 itens em fila era enganoso (era a média histórica do modelo).
  // O resumo do total continua visível no topo da tabela via _renderQueueSummary.
  let eta = '';
  if (f && status === 'processing') {
    const est = estimateRemainingSecs(f);
    if (est != null && est > 0) {
      eta = `<div class="row-eta">~${fmtSecs(est)} restante</div>`;
    } else {
      // Estimate exhausted but still running — mantém a linha estável.
      eta = `<div class="row-eta">finalizando…</div>`;
    }
  }
  return `<span class="status-badge ${s.cls}" role="img" aria-label="${label}"${extra}>
    <span class="status-dot"></span>${label}${pctLabel}
  </span>${eta}`;
}

// Signature used to decide whether a given row needs DOM work
function _rowSignature(f) {
  return [
    f.id, f.status, f.name, f.date, f.dur, f.mode, f.folder || '',
    f.source || '', f.has_original ? '1' : '0', f.url || '',
    selected.has(f.id) ? '1' : '0',
  ].join('|');
}

// Track last-rendered signatures so we can skip rebuilds when nothing changed.
const _renderedRowSigs = new Map();

function _buildRowInner(f) {
  return `
      <td class="col-check">
        <input type="checkbox" class="checkbox" aria-label="Selecionar ${esc(f.name)}"
          ${selected.has(f.id)?'checked':''}
          onclick="handleCheckboxClick('${jsAttr(f.id)}', this, event)" />
      </td>
      <td>
        <div class="file-name">${esc(f.name)}</div>
        <div class="file-tags">
          ${f.source === 'url'
            ? `<span class="ftag ftag-url" title="Adicionado via URL (yt-dlp)"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>URL</span>`
            : `<span class="ftag ftag-upload" title="Adicionado por upload"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>Upload</span>`}
          ${f.has_original
            ? `<span class="ftag ftag-orig-ok" title="O arquivo de áudio/vídeo original ainda está em uploads/"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>Original disponível</span>`
            : `<span class="ftag ftag-orig-gone" title="Arquivo original foi apagado — a transcrição continua disponível"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>Original apagado</span>`}
        </div>
      </td>
      <td class="col-date"><div class="file-date">${esc(f.date)}</div></td>
      <td class="col-dur"><div class="file-dur">${esc(f.dur)}</div></td>
      <td class="col-mode"><span class="mode-badge">${esc(f.mode)}</span></td>
      <td class="col-status">${renderStatus(f.status, f)}</td>
      <td class="col-actions">
        <div class="action-wrap">
          <button type="button" class="dots-btn" aria-label="Ações — ${esc(f.name)}" aria-haspopup="menu" aria-expanded="false"
            onclick="toggleDD('${jsAttr(f.id)}',this,event)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="5" r="1" fill="currentColor"/><circle cx="12" cy="12" r="1" fill="currentColor"/><circle cx="12" cy="19" r="1" fill="currentColor"/></svg>
          </button>
          <div class="dropdown" id="dd-${esc(f.id)}" role="menu">
            ${(f.status === 'queued' || f.status === 'processing') ? `
            <div class="dd-item danger" role="menuitem" tabindex="-1" onclick="cancelTranscriptionById('${jsAttr(f.task_id || '')}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
              Cancelar transcrição
            </div>` : f.status === 'error' ? `
            <div class="dd-item danger" role="menuitem" tabindex="-1" onclick="viewError('${jsAttr(f.id)}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
              Ver log de erro
            </div>
            <div class="dd-sep"></div>` : `
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="viewFile('${jsAttr(f.id)}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
              Ver transcrição
            </div>`}
            ${f.status === 'done' ? `
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="dlFile('${jsAttr(f.id)}','txt')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              Baixar TXT
            </div>
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="dlFile('${jsAttr(f.id)}','srt')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
              Baixar SRT (legendas)
            </div>
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="dlFile('${jsAttr(f.id)}','timestamps')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
              Baixar Timestamps
            </div>
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="dlFile('${jsAttr(f.id)}','json')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
              Baixar JSON
            </div>
            ${f.has_original ? `
            <div class="dd-sep"></div>
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="dlOriginalMedia('${jsAttr(f.file)}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
              Baixar arquivo original (áudio/vídeo)
            </div>
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="dlWithOriginal('${jsAttr(f.file)}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><line x1="10" y1="12" x2="14" y2="12"/></svg>
              Baixar transcrição + original (ZIP)
            </div>` : ''}
            <div class="dd-sep"></div>` : ''}
            ${f.url ? `
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="openOriginalLink('${jsAttr(f.url)}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
              Abrir link original
            </div>
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="copyOriginalLink('${jsAttr(f.url)}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
              Copiar link original
            </div>
            <div class="dd-sep"></div>` : ''}
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="promptMoveToFolder('${jsAttr(f.id)}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
              Mover para pasta${f.folder ? ` (${esc(f.folder)})` : ''}
            </div>
            <div class="dd-sep"></div>
            <div class="dd-item danger" role="menuitem" tabindex="-1" onclick="deleteFile('${jsAttr(f.id)}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>
              Excluir arquivo
            </div>
          </div>
        </div>
      </td>`;
}

let _tbodyDelegationAttached = false;
function _attachTbodyDelegation(tbody) {
  if (_tbodyDelegationAttached) return;
  // Event delegation — one listener on tbody survives re-renders
  tbody.addEventListener('click', e => {
    const tr = e.target.closest('tr');
    if (!tr) return;
    if (e.target.closest('.action-wrap') || e.target.closest('input[type="checkbox"]')) return;
    const id = tr.dataset.id;
    if (!id) return;
    // Clicking the error badge opens error viewer
    if (e.target.closest('.status-badge.error')) { viewError(id); return; }
    const f = files.find(x => x.id === id);
    if (f && f.status === 'error') { viewError(id); return; }
    if (f && (f.status === 'queued' || f.status === 'processing')) return;
    viewFile(id);
  });
  _tbodyDelegationAttached = true;
}

function renderFiles(dataOverride) {
  // If no explicit data passed, apply the current view pipeline.
  const data = dataOverride || _getVisibleFiles();
  const tbody = document.getElementById('files-tbody');
  const empty = document.getElementById('empty-state');
  const table = tbody.closest('table');

  _renderSortIndicators();
  _syncFolderButtonLabel();
  _renderStatusFilterCounts();
  _renderQueueSummary();
  _attachTbodyDelegation(tbody);

  if (!data.length) {
    tbody.innerHTML = '';
    _renderedRowSigs.clear();
    table.style.display = 'none';
    empty.style.display = 'flex';
    return;
  }
  table.style.display = '';
  empty.style.display = 'none';

  // Build a quick lookup of current DOM rows by id, plus the desired-order list.
  const currentRows = new Map();
  for (const tr of Array.from(tbody.children)) currentRows.set(tr.dataset.id, tr);

  const desiredIds = new Set();
  const newSigs = new Map();

  // Single pass: insert/update rows in desired order
  data.forEach((f, i) => {
    const id = f.id;
    desiredIds.add(id);
    const sig = _rowSignature(f);
    newSigs.set(id, sig);
    let tr = currentRows.get(id);
    if (!tr) {
      tr = document.createElement('tr');
      tr.dataset.id = id;
      tr.innerHTML = _buildRowInner(f);
    } else if (_renderedRowSigs.get(id) !== sig) {
      tr.innerHTML = _buildRowInner(f);
    }
    const existing = tbody.children[i];
    if (existing !== tr) tbody.insertBefore(tr, existing || null);
  });

  // Remove rows no longer in the view
  for (const [id, tr] of currentRows) {
    if (!desiredIds.has(id)) tr.remove();
  }

  _renderedRowSigs.clear();
  for (const [id, sig] of newSigs) _renderedRowSigs.set(id, sig);
}

// ═══════════════════════════════════════════════════════════════
//  SEARCH
// ═══════════════════════════════════════════════════════════════
function toggleSearch() {
  const bar = document.getElementById('search-bar');
  const btn = document.getElementById('search-toggle');
  const open = bar.classList.toggle('open');
  btn.classList.toggle('active', open);
  btn.setAttribute('aria-expanded', open);
  if (open) document.getElementById('search-input').focus();
  else { document.getElementById('search-input').value = ''; _view.search = ''; renderFiles(); }
}

function filterFiles(q) {
  _view.search = q || '';
  renderFiles();
}

// ═══════════════════════════════════════════════════════════════
//  SELECTION
// ═══════════════════════════════════════════════════════════════
let _lastCheckedId = null;

// Shift+click extends selection from the last clicked checkbox to this one,
// applying the current click's checked state to every row in between.
function handleCheckboxClick(id, cb, ev) {
  ev.stopPropagation();
  const checked = cb.checked; // native click has already toggled
  if (ev.shiftKey && _lastCheckedId && _lastCheckedId !== id) {
    const visible = _getVisibleFiles();
    const a = visible.findIndex(f => f.id === _lastCheckedId);
    const b = visible.findIndex(f => f.id === id);
    if (a !== -1 && b !== -1) {
      const [lo, hi] = a < b ? [a, b] : [b, a];
      for (let i = lo; i <= hi; i++) {
        const fid = visible[i].id;
        checked ? selected.add(fid) : selected.delete(fid);
      }
      // Shift+click in some browsers drags a text selection — clear it.
      try { window.getSelection().removeAllRanges(); } catch {}
      renderFiles();
      syncBulkBar();
      _lastCheckedId = id;
      return;
    }
  }
  checked ? selected.add(id) : selected.delete(id);
  _lastCheckedId = id;
  syncBulkBar();
  syncHeaderCheck();
}

function toggleSelect(id, cb) {
  cb.checked ? selected.add(id) : selected.delete(id);
  _lastCheckedId = id;
  syncBulkBar();
  syncHeaderCheck();
}

// Select/deselect respects the current filter — toggleAll only affects the
// visible rows, and header indeterminate state is computed against visible too.
function toggleAll(cb) {
  const visible = _getVisibleFiles();
  visible.forEach(f => cb.checked ? selected.add(f.id) : selected.delete(f.id));
  renderFiles();
  syncBulkBar();
}

function syncHeaderCheck() {
  const cb = document.getElementById('check-all');
  const visible = _getVisibleFiles();
  const visibleSelected = visible.filter(f => selected.has(f.id)).length;
  cb.checked = visibleSelected === visible.length && visible.length > 0;
  cb.indeterminate = visibleSelected > 0 && visibleSelected < visible.length;
}

// Returns only the selected IDs that are currently visible under the active
// filters — bulk actions should never touch rows the user can't see.
function selectedVisibleIds() {
  const visible = _getVisibleFiles();
  return visible.filter(f => selected.has(f.id)).map(f => f.id);
}

function syncBulkBar() {
  const bar = document.getElementById('bulk-bar');
  const count = selectedVisibleIds().length;
  document.getElementById('bulk-count').textContent =
    `${count} selecionado${count !== 1 ? 's' : ''}`;
  bar.classList.toggle('show', count > 0);
  syncHeaderCheck();
}

// ═══════════════════════════════════════════════════════════════
//  DROPDOWNS
// ═══════════════════════════════════════════════════════════════
// Track the trigger button so closeAllDDs() can restore focus
let _ddOpenTrigger = null;

function toggleDD(id, btn, e) {
  e.stopPropagation();
  const dd   = document.getElementById('dd-' + id);
  const open = dd.classList.contains('open');
  closeAllDDs();
  if (!open) {
    dd.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
    _ddOpenTrigger = btn;
    // Focus the first menuitem so the menu is usable with the keyboard
    const first = dd.querySelector('[role="menuitem"]');
    if (first) setTimeout(() => first.focus(), 0);
  }
}

function closeAllDDs(restoreFocus = false) {
  document.querySelectorAll('.dropdown.open').forEach(d => d.classList.remove('open'));
  document.querySelectorAll('.dots-btn[aria-expanded="true"]').forEach(b => b.setAttribute('aria-expanded','false'));
  if (restoreFocus && _ddOpenTrigger) {
    try { _ddOpenTrigger.focus(); } catch {}
  }
  _ddOpenTrigger = null;
}

// Keyboard navigation inside any open dropdown.
document.addEventListener('keydown', e => {
  const dd = document.querySelector('.dropdown.open');
  if (!dd) return;

  // Enter / Space activate the focused menuitem
  if ((e.key === 'Enter' || e.key === ' ') && dd.contains(document.activeElement)
      && document.activeElement.getAttribute('role') === 'menuitem') {
    e.preventDefault();
    document.activeElement.click();
    return;
  }

  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
  const items = Array.from(dd.querySelectorAll('[role="menuitem"]'))
    .filter(el => el.offsetParent !== null);
  if (!items.length) return;
  e.preventDefault();
  const cur = items.indexOf(document.activeElement);
  let nextIdx;
  if (e.key === 'ArrowDown') nextIdx = cur < 0 ? 0 : (cur + 1) % items.length;
  else if (e.key === 'ArrowUp') nextIdx = cur <= 0 ? items.length - 1 : cur - 1;
  else if (e.key === 'Home') nextIdx = 0;
  else nextIdx = items.length - 1;
  items[nextIdx].focus();
});

document.addEventListener('click', closeAllDDs);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    // If a dialog is open, cancel it and stop — we don't want ESC to also
    // close the modal/viewer/etc stacked behind it.
    const dialog = document.getElementById('dialog-overlay');
    if (dialog && dialog.classList.contains('open')) { _dialogCancel(); return; }
    closeAllDDs(); closeModal(); closeViewer(); closeFolderPicker(); closeSidebar(); closeSettings();
  }
});

// ═══════════════════════════════════════════════════════════════
//  FILE ACTIONS
// ═══════════════════════════════════════════════════════════════
async function viewFile(id) {
  const f = files.find(x => x.id === id);
  if (!f) return;
  if (f.status === 'error')      { viewError(id); return; }
  if (f.status === 'queued')     { showToast('Aguardando na fila…', ''); return; }
  if (f.status === 'processing') { showToast('Transcrição em andamento…', ''); return; }
  try {
    const res  = await fetch(`/api/result/${encodeURIComponent(f.file)}`);
    if (!res.ok) throw new Error();
    const data = await res.json();
    openViewer(f, data);
  } catch {
    showToast('Não foi possível carregar a transcrição.', 'error');
  }
}

function viewError(id) {
  closeAllDDs();
  const f = files.find(x => x.id === id);
  if (!f) return;
  const msg = f.error || 'Nenhuma mensagem de erro registrada.';
  // Reuse viewer modal to show the error log
  _viewerFile = f;
  _viewerData = {
    text: `❌ ERRO NA TRANSCRIÇÃO\n${'─'.repeat(50)}\nArquivo : ${f.file}\nModelo  : ${f.mode}\nData    : ${f.date}\n${'─'.repeat(50)}\n\n${msg}`,
    timestamped: '', srt: ''
  };
  document.getElementById('viewer-title').textContent = `Erro — ${f.name}`;
  // Hide tabs that aren't relevant for errors
  document.getElementById('vtab-ts').style.display  = 'none';
  document.getElementById('vtab-srt').style.display = 'none';
  _setViewerMeta(f, { errorMode: true });
  _renderViewerActions(f, { errorMode: true });
  switchViewerTab('text');
  document.getElementById('viewer-overlay').classList.add('open');
  document.body.style.overflow = 'hidden';
}

// Download the original audio/video file tied to a transcription.
// Same backend endpoint used by the Media Library tab. Probes existence with
// a tiny Range GET so we can show a clear toast if the user previously cleaned
// the original (the 7-day cleanup deletes the upload but keeps the transcription).
async function dlOriginalMedia(filename) {
  closeAllDDs();
  if (!filename) return;
  try {
    const probe = await fetch(`/api/download-media/${encodeURIComponent(filename)}`,
                              { headers: { 'Range': 'bytes=0-0' } });
    // 200 = ok, 206 = partial content (Range honored), both mean file exists
    if (probe.status !== 200 && probe.status !== 206) {
      showToast(
        probe.status === 404
          ? 'Arquivo original não está mais disponível (foi apagado da Biblioteca).'
          : 'Não foi possível baixar o arquivo original.',
        'error'
      );
      probe.body?.cancel?.();
      return;
    }
    probe.body?.cancel?.();
  } catch {
    showToast('Erro de rede ao verificar o arquivo.', 'error');
    return;
  }
  window.location = `/api/download-media/${encodeURIComponent(filename)}`;
}

// Download the transcription files AND the original media together in one ZIP.
function dlWithOriginal(filename) {
  closeAllDDs();
  if (!filename) return;
  window.location = `/api/download-with-original/${encodeURIComponent(filename)}`;
}

// Open the saved source link (yt-dlp URL) in a new tab.
function openOriginalLink(url) {
  closeAllDDs();
  if (!url) return;
  window.open(url, '_blank', 'noopener');
}

// Copy the saved source link to the clipboard.
async function copyOriginalLink(url) {
  closeAllDDs();
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
    showToast('Link copiado.', 'success');
  } catch {
    showToast('Não foi possível copiar o link.', 'error');
  }
}

function dlFile(id, fmt) {
  closeAllDDs();
  const f = files.find(x => x.id === id);
  if (!f) return;
  window.location = `/api/download/${encodeURIComponent(f.file)}/${fmt}`;
}

async function deleteFile(id) {
  closeAllDDs();
  const f = files.find(x => x.id === id);
  if (!f) return;
  const ok = await showConfirm({
    title: 'Excluir transcrição',
    message: `"${f.name}" será removido do histórico e todos os arquivos (TXT, SRT, JSON, timestamps) serão apagados do disco.`,
    confirmText: 'Excluir',
    danger: true
  });
  if (!ok) return;
  try {
    const res = await fetch(`/api/delete/${encodeURIComponent(f.file)}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('delete failed');
    files.splice(files.findIndex(x => x.id === id), 1);
    selected.delete(id);
    renderFiles();
    syncBulkBar();
    await loadStats();
    showToast('Arquivo excluído.', 'success');
  } catch {
    showToast('Erro ao excluir.', 'error');
  }
}

async function downloadSelected() {
  const ids = selectedVisibleIds();
  if (!ids.length) return;
  let n = 0;
  for (const id of ids) {
    const f = files.find(x => x.id === id);
    if (f && f.status === 'done') {
      const a = document.createElement('a');
      a.href = `/api/download/${encodeURIComponent(f.file)}/txt`;
      a.download = f.name + '.txt';
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      n++;
      await new Promise(r => setTimeout(r, 300));
    }
  }
  showToast(`${n} arquivo(s) baixado(s).`, 'success');
}

async function downloadAllZip() {
  const ids = selectedVisibleIds();
  if (!ids.length) {
    showToast('Selecione ao menos um arquivo.', 'error');
    return;
  }
  // Only include entries that actually have results on disk (status 'done')
  const filenames = ids
    .map(id => files.find(x => x.id === id))
    .filter(f => f && f.status === 'done')
    .map(f => f.file);
  if (!filenames.length) {
    showToast('Nenhum dos selecionados está pronto.', 'error');
    return;
  }

  // Ask which formats to include
  const formats = await showMultiChoice({
    title: 'Formatos no ZIP',
    message: `Escolha quais formatos incluir no ZIP de ${filenames.length} arquivo(s):`,
    iconKind: 'folder',
    confirmText: 'Gerar ZIP',
    options: [
      { value: 'txt',        label: 'TXT',          description: 'Texto limpo, sem marcação',  defaultChecked: true  },
      { value: 'srt',        label: 'SRT',          description: 'Legendas para vídeo',         defaultChecked: true  },
      { value: 'timestamps', label: 'Timestamps',   description: 'Texto com marcas de tempo',   defaultChecked: false },
      { value: 'json',       label: 'JSON',         description: 'Estrutura completa (avançado)', defaultChecked: false },
    ]
  });
  if (formats === null) return;            // cancelado
  if (!formats.length) {
    showToast('Selecione ao menos um formato.', 'error');
    return;
  }

  try {
    const body = new FormData();
    body.append('files', JSON.stringify(filenames));
    body.append('formats', formats.join(','));
    const res = await fetch('/api/download-selected-zip', { method: 'POST', body });
    if (!res.ok) {
      let msg = 'Erro ao gerar ZIP.';
      try { const e = await res.json(); if (e.detail) msg = e.detail; } catch {}
      showToast(msg, 'error');
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'transcricoes_selecionadas.zip';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast(`${filenames.length} arquivo(s) em ZIP (${formats.join(', ')}).`, 'success');
  } catch {
    showToast('Erro ao gerar ZIP.', 'error');
  }
}

async function deleteSelected() {
  const ids = selectedVisibleIds();
  if (!ids.length) return;
  const count = ids.length;
  const ok = await showConfirm({
    title: `Excluir ${count} transcriç${count === 1 ? 'ão' : 'ões'}`,
    message: `Os arquivos selecionados serão removidos permanentemente do histórico e do disco.`,
    confirmText: 'Excluir',
    danger: true
  });
  if (!ok) return;
  let failed = 0;
  for (const id of ids) {
    const f = files.find(x => x.id === id);
    if (f) {
      try {
        const res = await fetch(`/api/delete/${encodeURIComponent(f.file)}`, { method: 'DELETE' });
        if (!res.ok) { failed++; continue; }
        files.splice(files.findIndex(x => x.id === id), 1);
        selected.delete(id);
      } catch { failed++; }
    }
  }
  renderFiles();
  syncBulkBar();
  await loadStats();
  if (failed === 0) {
    showToast(`${count} arquivo(s) excluído(s).`, 'success');
  } else {
    showToast(`${count - failed} excluído(s), ${failed} falha(s).`, 'error');
  }
}

// ═══════════════════════════════════════════════════════════════
//  UPLOAD MODAL
// ═══════════════════════════════════════════════════════════════
function openModal(tab = 'file') {
  // Reset BOTH mode-switches to the default ("transcribe") on every open so
  // the user doesn't inherit a previous session's "Apenas baixar" selection.
  document.querySelectorAll('#overlay .mode-opt').forEach(b => {
    b.setAttribute('aria-checked', String(b.dataset.mode === 'transcribe'));
  });
  switchTab(tab);
  _populateFolderSelect();
  document.getElementById('overlay').classList.add('open');
  document.body.style.overflow = 'hidden';
  attachFocusTrap('overlay');
  setTimeout(() => document.querySelector('#overlay .modal-close').focus(), 50);
}

// Fill the "Salvar na pasta" dropdown from the known folder list, preserving
// the current selection when possible. Pre-selects the folder currently being
// viewed (if the user has a folder filter active) so "transcribe here" is the default.
function _populateFolderSelect() {
  const sel = document.getElementById('folder-select');
  if (!sel) return;
  const prev = sel.value;
  const activeFolder = (_view.folderFilter && !['all', '__root__'].includes(_view.folderFilter))
    ? _view.folderFilter : '';
  const paths = (_folders || []).map(f => f.path).sort();
  sel.innerHTML = '<option value="">Sem pasta (raiz)</option>'
    + paths.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
  // Restore previous choice, else default to the folder currently in view.
  sel.value = paths.includes(prev) ? prev : (paths.includes(activeFolder) ? activeFolder : '');

  // Belt-and-suspenders: if the folder list hasn't loaded yet (modal opened
  // before init finished), fetch it and re-populate once it arrives.
  if (!paths.length) {
    loadFolders().then(() => {
      if ((_folders || []).length) _populateFolderSelect();
    });
  }
}

function closeModal() {
  document.getElementById('overlay').classList.remove('open');
  document.body.style.overflow = '';
  detachFocusTrap('overlay');
}

function handleOverlayClick(e) {
  if (e.target === document.getElementById('overlay')) closeModal();
}

function switchTab(tab) {
  ['file', 'url', 'batch'].forEach(t => {
    const selected = t === tab;
    const btn = document.getElementById('tab-' + t);
    if (!btn) return;
    btn.setAttribute('aria-selected', selected);
    btn.tabIndex = selected ? 0 : -1;
    document.getElementById('content-' + t).style.display = selected ? '' : 'none';
  });

  // Apply mode for the new tab — reads from the .mode-switch cards (file is always transcribe)
  setModalMode(tab === 'file' ? 'transcribe'
              : (document.querySelector(`#content-${tab} .mode-opt[aria-checked="true"]`)?.dataset.mode || 'transcribe'));

  // Visibility of the old #download-only-btn (kept as no-op for safety; the
  // single transcribe-btn now handles both actions via setModalMode's label).
  const dlBtn = document.getElementById('download-only-btn');
  if (dlBtn) dlBtn.style.display = 'none';

  const txBtn = document.getElementById('transcribe-btn');
  if (txBtn) {
    txBtn.style.display = 'block';
    if (tab === 'file') {
      txBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/></svg>TRANSCREVER';
    }
  }
}

// Roving-tabindex keyboard handler for tablists (ArrowLeft/Right/Home/End).
// Group = 'main' for the page tabs, 'modal' for the upload modal tabs.
const _TABLIST_GROUPS = {
  main:  ['main-tab-transcriptions', 'main-tab-media'],
  modal: ['tab-file', 'tab-url', 'tab-batch'],
};
function onTablistKey(e, group) {
  const ids = _TABLIST_GROUPS[group];
  if (!ids) return;
  const idx = ids.indexOf(e.currentTarget.id);
  let next = -1;
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (idx + 1) % ids.length;
  else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (idx - 1 + ids.length) % ids.length;
  else if (e.key === 'Home') next = 0;
  else if (e.key === 'End') next = ids.length - 1;
  if (next < 0) return;
  e.preventDefault();
  const target = document.getElementById(ids[next]);
  target.focus();
  target.click(); // activate on arrow (matches automatic-activation pattern)
}

function switchMainTab(tab) {
  ['transcriptions', 'media'].forEach(t => {
    const pressed = t === tab;
    const btn = document.getElementById('main-tab-' + t);
    btn.setAttribute('aria-selected', pressed);
    btn.setAttribute('aria-pressed', pressed); // legacy — kept for any callers reading it
    btn.tabIndex = pressed ? 0 : -1;
    // Use '' to clear the inline style so the element falls back to its CSS
    // rule. #card-transcriptions is .card-with-sidebar (display:grid on desktop,
    // block on mobile via media query); forcing 'block' would break the grid.
    document.getElementById('card-' + t).style.display = pressed ? '' : 'none';
  });
  if (tab === 'media') {
    loadMedia();
  } else {
    // Switching away from media — drop any background media-refresh timers
    // that were keeping the list in sync during downloads.
    if (typeof _mediaRefreshIntervals !== 'undefined') {
      _mediaRefreshIntervals.forEach(id => clearInterval(id));
      _mediaRefreshIntervals.clear();
    }
  }
}

function formatBytes(bytes, decimals = 1) {
    if (!+bytes) return '0 Bytes';
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
}

// ═══════════════════════════════════════════════════════════════
//  DROPZONE
// ═══════════════════════════════════════════════════════════════
function handleDragOver(e) { e.preventDefault(); document.getElementById('dropzone').classList.add('drag-over'); }
function handleDragLeave()  { document.getElementById('dropzone').classList.remove('drag-over'); }
function handleDrop(e) {
  e.preventDefault();
  document.getElementById('dropzone').classList.remove('drag-over');
  if (e.dataTransfer.files.length) handleFileSelect(e.dataTransfer.files);
}

function handleFileSelect(fileList) {
  if (!fileList || !fileList.length) return;
  pendingFiles = Array.from(fileList);
  const f = pendingFiles[0];
  document.getElementById('chip-name').textContent =
    pendingFiles.length > 1 ? `${f.name} + ${pendingFiles.length-1} outro(s)` : f.name;
  document.getElementById('chip-size').textContent =
    pendingFiles.reduce((s, x) => s + x.size, 0) < 1048576
      ? (pendingFiles.reduce((s, x) => s + x.size, 0) / 1024).toFixed(1) + ' KB'
      : (pendingFiles.reduce((s, x) => s + x.size, 0) / 1048576).toFixed(1) + ' MB';
  document.getElementById('file-chip').classList.add('show');
  document.getElementById('dropzone').style.display = 'none';
}

function removeFile() {
  pendingFiles = [];
  document.getElementById('file-input').value = '';
  document.getElementById('file-chip').classList.remove('show');
  document.getElementById('dropzone').style.display = '';
}

// ═══════════════════════════════════════════════════════════════
//  BATCH IMPORT — CSV / TXT planilha → muitas URLs de uma vez
// ═══════════════════════════════════════════════════════════════
const _URL_RE = /\bhttps?:\/\/[^\s,"'<>() ]+/g;

// Detects URLs from arbitrary text (CSV, TSV, TXT, paste). Returns deduped list
// preserving order. Strips surrounding quotes/whitespace that spreadsheets add.
function _extractUrlsFromText(text) {
  if (!text) return [];
  const matches = text.match(_URL_RE) || [];
  const seen = new Set();
  const out = [];
  for (const m of matches) {
    // CSV often wraps URLs in quotes — match doesn't include them but trailing
    // punctuation can sneak in. Trim common offenders.
    const cleaned = m.replace(/[)\]}>,;.'"]+$/g, '');
    if (cleaned && !seen.has(cleaned)) { seen.add(cleaned); out.push(cleaned); }
  }
  return out;
}

function _parseBatchUrls() {
  // Source of truth: textarea (file uploads populate it, paste is direct)
  const text = document.getElementById('batch-urls-input')?.value || '';
  return _extractUrlsFromText(text);
}

function updateBatchCount() {
  const urls = _parseBatchUrls();
  const summary = document.getElementById('batch-summary');
  const count   = document.getElementById('batch-count');
  const extra   = document.getElementById('batch-extra');
  if (!summary || !count) return;
  if (!urls.length) { summary.style.display = 'none'; return; }
  summary.style.display = 'block';
  count.textContent = urls.length;
  // Show a hint with the first host so the user knows we parsed correctly
  try {
    const hosts = new Map();
    for (const u of urls) {
      const h = new URL(u).hostname;
      hosts.set(h, (hosts.get(h) || 0) + 1);
    }
    const top = [...hosts.entries()].sort((a,b) => b[1] - a[1]).slice(0, 3);
    extra.textContent = ' · ' + top.map(([h,c]) => `${c}× ${h}`).join(', ');
  } catch { extra.textContent = ''; }
}

async function handleBatchFile(fileList) {
  const file = fileList && fileList[0];
  if (!file) return;
  // CSV/TXT/TSV — read as text; URLs extracted regardless of structure
  if (file.size > 20 * 1024 * 1024) {
    showToast('Arquivo > 20 MB — exporte só a coluna de URLs.', 'error');
    return;
  }
  try {
    const text = await file.text();
    const urls = _extractUrlsFromText(text);
    if (!urls.length) {
      showToast('Nenhuma URL http(s):// encontrada no arquivo.', 'error');
      return;
    }
    document.getElementById('batch-urls-input').value = urls.join('\n');
    updateBatchCount();
    showToast(`${urls.length} URLs carregadas de "${file.name}"`, 'success');
  } catch {
    showToast('Não consegui ler o arquivo.', 'error');
  }
}

function handleBatchDragOver(e) { e.preventDefault(); document.getElementById('batch-dropzone').classList.add('drag-over'); }
function handleBatchDragLeave()  { document.getElementById('batch-dropzone').classList.remove('drag-over'); }
function handleBatchDrop(e) {
  e.preventDefault();
  document.getElementById('batch-dropzone').classList.remove('drag-over');
  if (e.dataTransfer.files.length) handleBatchFile(e.dataTransfer.files);
}

// Returns the active modal tab name ('file' | 'url' | 'batch') by reading
// the seg-tab buttons inside the upload overlay. Used by mode logic.
function _activeModalTab() {
  return document.querySelector('#overlay .seg-tab[aria-selected="true"]')
                 ?.id?.replace('tab-', '') || 'file';
}

// Read the currently-selected mode for the active tab. Arquivo is always
// 'transcribe' (no download choice — the file is already local).
function _currentModalMode() {
  const tab = _activeModalTab();
  if (tab === 'file') return 'transcribe';
  const opt = document.querySelector(`#content-${tab} .mode-opt[aria-checked="true"]`);
  return opt?.dataset.mode || 'transcribe';
}

// Switch between 'transcribe' and 'download' for the active URL/Lote tab.
// Updates the visual card selection, hides/shows mode-specific option panels,
// hides transcribe-only fields (model/lang/folder/advanced) in download mode,
// and relabels the primary action button.
function setModalMode(mode) {
  const tab = _activeModalTab();
  if (tab === 'file') mode = 'transcribe';  // forced — see above

  // 1. Update the cards in THIS tab's .mode-switch
  document.querySelectorAll(`#content-${tab} .mode-opt`).forEach(btn => {
    btn.setAttribute('aria-checked', String(btn.dataset.mode === mode));
  });

  // 2. Toggle mode-specific options
  const urlDl   = document.getElementById('url-dl-options');
  const batchDl = document.getElementById('batch-dl-options');
  if (urlDl)   urlDl.style.display   = (tab === 'url'   && mode === 'download') ? 'block' : 'none';
  if (batchDl) batchDl.style.display = (tab === 'batch' && mode === 'download') ? 'block' : 'none';

  // 3. Model/lang/folder/advanced only matter when actually transcribing
  const transcribeFields = document.getElementById('transcribe-fields');
  if (transcribeFields) transcribeFields.style.display = (mode === 'download') ? 'none' : '';

  // 4. Relabel the primary action button to match what's about to happen
  const txBtn = document.getElementById('transcribe-btn');
  if (txBtn) {
    const labels = {
      'file|transcribe':   'TRANSCREVER',
      'url|transcribe':    'TRANSCREVER',
      'url|download':      'BAIXAR',
      'batch|transcribe':  'TRANSCREVER LOTE',
      'batch|download':    'BAIXAR LOTE',
    };
    const isDownload = mode === 'download';
    const icon = isDownload
      ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/></svg>';
    txBtn.innerHTML = icon + (labels[`${tab}|${mode}`] || 'TRANSCREVER');
  }
}

// Back-compat shim — old call sites used updateBatchMode(); now route through setModalMode
function updateBatchMode() { setModalMode(_currentModalMode()); }

// Mirror of updateQualityOptions for the batch tab's own selects.
function updateBatchQualityOptions() {
  const type = document.getElementById('batch-media-type')?.value;
  const q    = document.getElementById('batch-quality');
  if (!q) return;
  q.innerHTML = type === 'video'
    ? `<option value="best">Melhor (Máxima)</option>
       <option value="1080p">1080p</option>
       <option value="720p">720p</option>
       <option value="480p">480p</option>`
    : `<option value="best">Melhor Original</option>
       <option value="worst">Menor Espaço</option>`;
}

async function dispatchBatch(urls, model, language, taskType, filterFillers, folder) {
  // Mode now comes from the visual cards (.mode-opt), not a hidden checkbox.
  const downloadOnly = (document.querySelector('#content-batch .mode-opt[aria-checked="true"]')?.dataset.mode === 'download');
  const mediaType    = document.getElementById('batch-media-type')?.value || 'video';
  const quality      = document.getElementById('batch-quality')?.value    || 'best';
  const verb         = downloadOnly ? 'downloads' : 'transcrições';
  showToast(`Enfileirando ${urls.length} ${verb}…`, '');
  try {
    const fd = new FormData();
    fd.append('urls',           urls.join('\n'));
    fd.append('model',          model);
    fd.append('language',       language);
    fd.append('task',           taskType);
    fd.append('filter_fillers', filterFillers ? 'true' : 'false');
    fd.append('folder',         folder);
    fd.append('transcribe',     downloadOnly ? 'false' : 'true');
    fd.append('media_type',     mediaType);
    fd.append('quality',        quality);
    const r = await fetch('/api/transcribe-batch', { method: 'POST', body: fd });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { showToast(data.detail || 'Erro ao enfileirar lote.', 'error'); return; }
    showToast(`${data.submitted}/${data.total} ${verb} enfileirad${verb.endsWith('s') ? 'as' : 'os'}!`, 'success');
    // Clear the textarea so the user knows it landed
    const ta = document.getElementById('batch-urls-input');
    if (ta) { ta.value = ''; updateBatchCount(); }
    if (downloadOnly) {
      // Download-only items live in the Media Library tab, not Transcriptions
      switchMainTab('media');
      if (typeof loadMedia === 'function') loadMedia();
    } else {
      await loadHistory();
      // Hook polling for the first handful so the user gets immediate visual feedback
      // (the rest will be picked up by resumeActivePolling on next tick / auto-sync).
      for (const tid of (data.task_ids || []).slice(0, 5)) {
        const f = files.find(x => x.task_id === tid);
        if (f) pollProgressForRow(tid, f.file);
      }
    }
  } catch {
    showToast('Erro de rede ao enfileirar lote.', 'error');
  }
}

// ═══════════════════════════════════════════════════════════════
//  ADVANCED
// ═══════════════════════════════════════════════════════════════
function toggleAdvanced() {
  const panel = document.getElementById('adv-panel');
  const btn   = document.getElementById('adv-toggle');
  const open  = panel.classList.toggle('open');
  btn.classList.toggle('open', open);
  btn.setAttribute('aria-expanded', open);
}

// ═══════════════════════════════════════════════════════════════
//  TRANSCRIPTION
// ═══════════════════════════════════════════════════════════════
async function startTranscription() {
  // Scope to the upload modal — otherwise this picks up the main page tab
  // (#main-tab-transcriptions), which makes URL and Recording silently fall
  // back to the "file" branch.
  const activeTab = document.querySelector('#overlay .seg-tab[aria-selected="true"]')
                     ?.id?.replace('tab-','') || 'file';

  const folder = document.getElementById('folder-select')?.value || '';

  if (activeTab === 'url') {
    const mode = _currentModalMode();
    const urlVal = document.getElementById('url-input')?.value?.trim();
    if (!urlVal) { showToast('Cole uma URL válida.', 'error'); return; }

    if (mode === 'download') {
      // Existing single-URL download-only path (it closes the modal itself).
      await startDownloadOnly();
      return;
    }
    // Transcribe path
    const model         = document.getElementById('mode-select').value;
    const language      = document.getElementById('lang-select').value;
    const taskType      = document.getElementById('task-select').value;
    const filterFillers = document.getElementById('filter-toggle').checked;
    closeModal();
    await sendUrl(urlVal, model, language, taskType, filterFillers, folder);
    return;
  }

  if (activeTab === 'batch') {
    const urls = _parseBatchUrls();
    if (!urls.length) { showToast('Nenhuma URL detectada — selecione um arquivo ou cole URLs.', 'error'); return; }
    // model/lang/folder only matter for transcribe mode; dispatchBatch reads the
    // mode from the cards and skips these for download-only.
    const model         = document.getElementById('mode-select')?.value   || 'turbo';
    const language      = document.getElementById('lang-select')?.value   || 'pt';
    const taskType      = document.getElementById('task-select')?.value   || 'transcribe';
    const filterFillers = document.getElementById('filter-toggle')?.checked || false;
    closeModal();
    await dispatchBatch(urls, model, language, taskType, filterFillers, folder);
    return;
  }

  if (!pendingFiles.length) {
    showToast('Selecione um arquivo primeiro.', 'error');
    return;
  }

  const model         = document.getElementById('mode-select').value;
  const language      = document.getElementById('lang-select').value;
  const taskType      = document.getElementById('task-select').value;
  const filterFillers = document.getElementById('filter-toggle').checked;

  closeModal();

  // Queue all pending files
  for (const file of pendingFiles) {
    await sendFile(file, model, language, taskType, filterFillers, folder);
  }
  removeFile();
}

async function sendFile(file, model, language, taskType, filterFillers, folder = '') {
  const fd = new FormData();
  fd.append('file',           file);
  fd.append('model',          model);
  fd.append('language',       language);
  fd.append('task',           taskType);
  fd.append('filter_fillers', filterFillers ? 'true' : 'false');
  fd.append('folder',         folder);

  showProgress(file.name);   // task_id known only after the POST returns

  try {
    const res = await fetch('/api/transcribe', { method: 'POST', body: fd });
    if (!res.ok) {
      let msg = '';
      try { const errData = await res.json(); msg = errData.detail || ''; } catch {}
      hideProgress();
      showToast(`Erro ao enviar "${file.name}"${msg ? ': ' + msg : ''}.`, 'error');
      return;
    }
    const data = await res.json();
    if (!data.task_id) {
      hideProgress();
      showToast(`Erro ao enviar "${file.name}": resposta inválida do servidor.`, 'error');
      return;
    }
    // Reload history immediately so item appears in table as "Aguardando"
    await loadHistory();
    pollProgressForRow(data.task_id, data.filename || file.name);
  } catch {
    hideProgress();
    showToast(`Erro ao enviar "${file.name}".`, 'error');
  }
}

// ═══════════════════════════════════════════════════════════════
//  PROGRESS POLLING
// ═══════════════════════════════════════════════════════════════
// The old top progress bar was removed — progress is shown inline in each
// row's status badge (see _updateRowStatus). These remain as safe no-ops so
// the existing call sites keep working without a separate bar element.
function showProgress(_name, _taskId) { /* no-op: per-row badge shows progress */ }
function hideProgress() { /* no-op */ }
function setProgressPct(_pct) { /* no-op */ }

// Cancel a specific transcription by task_id (used by the per-row action menu).
async function cancelTranscriptionById(taskId) {
  closeAllDDs();
  if (!taskId) return;
  const ok = await showConfirm({
    title: 'Cancelar transcrição',
    message: 'Tarefas ainda na fila são canceladas imediatamente. Para a que já está rodando, o ciclo atual termina e o resultado é descartado.',
    confirmText: 'Cancelar transcrição',
    cancelText: 'Continuar',
    danger: true
  });
  if (!ok) return;
  try {
    const res = await fetch(`/api/transcribe/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
    if (!res.ok) { showToast('Não foi possível cancelar.', 'error'); return; }
    _stopPoll(taskId);
    await loadHistory();
    showToast('Transcrição cancelada.', '');
  } catch {
    showToast('Erro de rede ao cancelar.', 'error');
  }
}

// Per-row polling — updates the table row live without full re-render
const _activePolls = {}; // task_id → intervalId

function pollProgressForRow(task_id, filename) {
  if (_activePolls[task_id]) return; // already polling

  _activePolls[task_id] = setInterval(async () => {
    try {
      const res  = await fetch(`/api/progress/${task_id}`);
      if (!res.ok) { _stopPoll(task_id); return; }
      const data = await res.json();

      // Push phase + phase_progress + overall progress to the row state, so the
      // badge can show "Baixando 35%" → "Transcrevendo 67%" → "Salvando…".
      _updateRowStatus(filename, data.status, data.progress || 0,
                       data.phase, data.phase_progress);

      if (data.status === 'done') {
        _stopPoll(task_id);
        hideProgress();
        await loadHistory();
        await loadStats();
        if (typeof loadMedia === 'function') loadMedia();
        // Download-only tasks don't have a history entry — distinguish here so
        // the toast matches what actually happened.
        const isTranscribe = files.some(f => f.file === filename);
        showToast(
          isTranscribe ? `"${data.filename || filename}" transcrito com sucesso!`
                       : 'Download concluído.',
          'success'
        );
      } else if (data.status === 'error') {
        _stopPoll(task_id);
        hideProgress();
        await loadHistory(); // reload so error message appears in row
        if (typeof loadMedia === 'function') loadMedia();
        const errMsg = data.error || 'Falha';
        showToast(`Erro em "${filename}": ${errMsg}`, 'error');
      } else if (data.status === 'cancelled') {
        _stopPoll(task_id);
        hideProgress();
        await loadHistory();
        if (typeof loadMedia === 'function') loadMedia();
      }
    } catch { /* ignore network blip */ }
  }, 800);
}

function _stopPoll(task_id) {
  if (_activePolls[task_id]) {
    clearInterval(_activePolls[task_id]);
    delete _activePolls[task_id];
  }
}

function _updateRowStatus(filename, status, pct, phase, phasePct) {
  // Persist live progress + phase on the in-memory entry so EVERY render path
  // (this poll, the 5s ETA tick, and full re-renders) produces identical output —
  // the % and ETA stay visible the whole time instead of flickering.
  const f = files.find(x => x.file === filename);
  if (f) {
    f.status = status;
    f._progress = pct;
    if (phase) f._phase = phase;
    if (typeof phasePct === 'number') f._phaseProgress = phasePct;
  }
  const tr = document.querySelector(`#files-tbody tr[data-id="${CSS.escape(filename)}"]`);
  if (tr) {
    const cell = tr.querySelector('.col-status');
    if (cell) cell.innerHTML = renderStatus(status, f || { _progress: pct, _phase: phase, _phaseProgress: phasePct });
  }
  // Mirror to the media library tab — download-only tasks live there.
  const mediaTr = document.querySelector(`#media-tbody tr[data-id="${CSS.escape(filename)}"]`);
  if (mediaTr) {
    const mcell = mediaTr.querySelector('.col-status');
    if (mcell) mcell.innerHTML = renderStatus(status, f || { _progress: pct, _phase: phase, _phaseProgress: phasePct });
  }
}

// ═══════════════════════════════════════════════════════════════
//  VIEWER MODAL
// ═══════════════════════════════════════════════════════════════
function openViewer(f, data) {
  _viewerFile = f;
  _viewerData = data;
  document.getElementById('viewer-title').textContent = f.name;
  // Ensure all tabs visible (may have been hidden by viewError)
  document.getElementById('vtab-ts').style.display  = '';
  document.getElementById('vtab-srt').style.display = '';
  _setViewerMeta(f);
  _renderViewerActions(f);
  switchViewerTab('text');
  document.getElementById('viewer-overlay').classList.add('open');
  document.body.style.overflow = 'hidden';
  attachFocusTrap('viewer-overlay');
  setTimeout(() => document.querySelector('#viewer-overlay .modal-close')?.focus(), 50);
}

// Build the viewer's action row so it mirrors the row's three-dots menu:
// the four format downloads plus original/ZIP/link/move/delete when applicable.
function _renderViewerActions(f, { errorMode = false } = {}) {
  const row = document.getElementById('viewer-dl-row');
  if (!row) return;
  const ic = {
    dl:   '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
    orig: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>',
    zip:  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><line x1="10" y1="12" x2="14" y2="12"/></svg>',
    link: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>',
    copy: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    move: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
    del:  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>',
  };
  const sections = [];

  // 1. Transcription downloads — TXT emphasized, other formats as soft chips.
  //    Skipped for error items (no transcription files exist on disk).
  if (!errorMode) {
    sections.push(`
      <div class="va-section">
        <span class="va-label">Transcrição</span>
        <div class="va-btns">
          <button type="button" class="btn btn-primary" onclick="dlViewerFile('txt')">${ic.dl}Baixar TXT</button>
          <button type="button" class="btn btn-soft" onclick="dlViewerFile('srt')">SRT</button>
          <button type="button" class="btn btn-soft" onclick="dlViewerFile('timestamps')">Timestamps</button>
          <button type="button" class="btn btn-soft" onclick="dlViewerFile('json')">JSON</button>
        </div>
      </div>`);
  }

  // 2. Original media + source link — side-by-side groups, each shown only if relevant.
  const cols = [];
  if (f.has_original) {
    cols.push(`
      <div class="va-section">
        <span class="va-label">Arquivo original</span>
        <div class="va-btns">
          <button type="button" class="btn btn-soft" onclick="dlOriginalMedia('${jsAttr(f.file)}')">${ic.orig}Áudio/vídeo</button>
          <button type="button" class="btn btn-soft" onclick="dlWithOriginal('${jsAttr(f.file)}')">${ic.zip}Tudo em ZIP</button>
        </div>
      </div>`);
  }
  if (f.url) {
    cols.push(`
      <div class="va-section">
        <span class="va-label">Link de origem</span>
        <div class="va-btns">
          <button type="button" class="btn btn-soft" onclick="openOriginalLink('${jsAttr(f.url)}')">${ic.link}Abrir</button>
          <button type="button" class="btn btn-soft" onclick="copyOriginalLink('${jsAttr(f.url)}')">${ic.copy}Copiar</button>
        </div>
      </div>`);
  }
  if (cols.length) sections.push(`<div class="va-cols">${cols.join('')}</div>`);

  // 3. Manage — separated from downloads; delete is destructive and right-aligned.
  if (sections.length) sections.push('<div class="va-div"></div>');
  sections.push(`
    <div class="va-manage">
      <button type="button" class="btn btn-soft" onclick="promptMoveToFolder('${jsAttr(f.id)}')">${ic.move}Mover para pasta</button>
      <button type="button" class="btn btn-soft danger" onclick="deleteViewerFile()">${ic.del}Excluir</button>
    </div>`);

  row.innerHTML = sections.join('');
}

// Compact context line under the viewer title: duration · language · word count.
function _setViewerMeta(f, { errorMode = false } = {}) {
  const el = document.getElementById('viewer-meta');
  if (!el) return;
  const bits = [];
  if (errorMode) {
    bits.push('Falha na transcrição');
  } else {
    if (f.dur && f.dur !== '—') bits.push(esc(f.dur));
    if (f.lang) bits.push(esc(String(f.lang).toUpperCase()));
    if (f.words) bits.push(`${f.words.toLocaleString('pt-BR')} palavras`);
  }
  el.innerHTML = bits.join('<span class="vm-dot"></span>');
}

// Delete from inside the viewer, then close it if the item is really gone.
async function deleteViewerFile() {
  const f = _viewerFile;
  if (!f) return;
  await deleteFile(f.id);
  if (!files.find(x => x.id === f.id)) closeViewer();
}

function closeViewer() {
  document.getElementById('viewer-overlay').classList.remove('open');
  document.body.style.overflow = '';
  detachFocusTrap('viewer-overlay');
}

function handleViewerOverlayClick(e) {
  if (e.target === document.getElementById('viewer-overlay')) closeViewer();
}

function switchViewerTab(tab) {
  const map = { text: 'text', ts: 'timestamped', srt: 'srt', gaps: '' };
  
  const isGaps = tab === 'gaps';
  document.getElementById('viewer-text').style.display = isGaps ? 'none' : 'block';
  document.getElementById('gaps-panel').style.display = isGaps ? 'flex' : 'none';
  
  if (!isGaps) {
    document.getElementById('viewer-text').value = _viewerData[map[tab]] || '';
  } else {
    if (_viewerFile) loadGaps(_viewerFile.file);
  }

  ['text','ts','srt','gaps'].forEach(t => {
    const el = document.getElementById('vtab-' + t);
    if (!el) return;
    el.classList.toggle('active', t === tab);
    el.setAttribute('aria-selected', t === tab);
  });
}

function dlViewerFile(fmt) {
  if (!_viewerFile) return;
  window.location = `/api/download/${encodeURIComponent(_viewerFile.file)}/${fmt}`;
}

// ═══════════════════════════════════════════════════════════════
//  TOAST
// ═══════════════════════════════════════════════════════════════
const TOAST_ICONS = {
  success: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>`,
  error:   `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
  '':      `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
};

function showToast(msg, type = '') {
  const area = document.getElementById('toast-area');
  const t    = document.createElement('div');
  t.className = 'toast' + (type ? ' ' + type : '');
  t.setAttribute('role', 'alert');
  // Icon is trusted static HTML; msg may contain user/backend strings, so use textContent.
  t.innerHTML = TOAST_ICONS[type] || TOAST_ICONS[''];
  const span = document.createElement('span');
  span.textContent = msg;
  t.appendChild(span);
  area.appendChild(t);
  setTimeout(() => t.remove(), 4500);
}

// ═══════════════════════════════════════════════════════════════
//  CLEANUP — warn about audio/video files older than 7 days
// ═══════════════════════════════════════════════════════════════
// Retention rule: source audio/video files in .whisper_data/uploads/ may be
// flagged for deletion after 7 days. Transcriptions are NEVER touched by this
// flow — only user-initiated transcription deletion cascades to the upload.
const _CLEANUP_RETENTION_DAYS = 7;
const _CLEANUP_DISMISS_KEY    = 'wt:cleanup-dismissed-until';
let _cleanupOldItems = []; // cached list from server for the "Apagar agora" action

function _formatBytes(bytes) {
  if (!bytes || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${units[i]}`;
}

async function checkOldMediaCleanup() {
  // Skip if the user dismissed the banner recently (24h cooldown)
  try {
    const until = parseFloat(localStorage.getItem(_CLEANUP_DISMISS_KEY) || '0');
    if (until && Date.now() < until) return;
  } catch { /* localStorage may be blocked in some contexts — proceed anyway */ }

  try {
    const res = await fetch(`/api/media/older-than?days=${_CLEANUP_RETENTION_DAYS}`);
    if (!res.ok) return;
    const data = await res.json();
    if (!data.count) return;

    _cleanupOldItems = data.items || [];
    const headline = document.getElementById('cleanup-headline');
    const sizeEl   = document.getElementById('cleanup-size');
    if (headline) {
      headline.textContent = `${data.count} ${data.count === 1 ? 'arquivo' : 'arquivos'} `
        + `de áudio/vídeo com mais de ${_CLEANUP_RETENTION_DAYS} dias`;
    }
    if (sizeEl) sizeEl.textContent = _formatBytes(data.total_bytes);
    document.getElementById('cleanup-banner').style.display = 'flex';
  } catch { /* network blip — just don't show the banner this time */ }
}

function dismissCleanupBanner() {
  // Dismiss for 24h so the user isn't nagged every page refresh.
  try {
    localStorage.setItem(_CLEANUP_DISMISS_KEY, String(Date.now() + 24 * 3600 * 1000));
  } catch {}
  const banner = document.getElementById('cleanup-banner');
  if (banner) banner.style.display = 'none';
}

async function runOldMediaCleanup() {
  if (!_cleanupOldItems.length) { dismissCleanupBanner(); return; }
  const totalBytes = _cleanupOldItems.reduce((s, x) => s + (x.size_bytes || 0), 0);
  const ok = await showConfirm({
    title: `Apagar ${_cleanupOldItems.length} arquivos antigos?`,
    message: `Vai liberar ~${_formatBytes(totalBytes)} de espaço. `
           + `Apenas os arquivos originais de áudio/vídeo serão removidos — `
           + `as transcrições continuam disponíveis para visualização e download.`,
    confirmText: 'Apagar arquivos',
    cancelText:  'Cancelar',
    danger: true,
  });
  if (!ok) return;

  const btn = document.getElementById('cleanup-confirm-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Apagando…'; }
  try {
    const fd = new FormData();
    fd.append('files', _cleanupOldItems.map(x => x.file).join(','));
    const res = await fetch('/api/media/cleanup', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(data.detail || 'Erro ao apagar arquivos.', 'error');
      return;
    }
    showToast(
      `${data.deleted} ${data.deleted === 1 ? 'arquivo apagado' : 'arquivos apagados'}` +
      ` — ${_formatBytes(data.freed_bytes)} liberados.`,
      'success'
    );
    document.getElementById('cleanup-banner').style.display = 'none';
    _cleanupOldItems = [];
    // Refresh media list in case the user has the Library tab open
    if (typeof loadMedia === 'function') loadMedia();
  } catch {
    showToast('Erro de rede ao apagar arquivos.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Apagar agora'; }
  }
}

// ═══════════════════════════════════════════════════════════════
//  VISIBILITY — pause polling when tab is hidden
// ═══════════════════════════════════════════════════════════════
// Track each poll's (task_id, filename) so we can resume after tab returns.
const _pausedPolls = new Map();

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    // Snapshot active polls, then stop them. Auto-sync (5s) and ETA tick
    // also pause via the same hidden flag (checked inside _autoSyncTick).
    for (const taskId of Object.keys(_activePolls)) {
      // Recover the row filename from the in-memory history (keyed by task_id).
      const fname = (files.find(f => f.task_id === taskId) || {}).file || '';
      _pausedPolls.set(taskId, fname);
      _stopPoll(taskId);
    }
  } else {
    // Resume polls that were active when we went hidden.
    for (const [taskId, fname] of _pausedPolls) {
      pollProgressForRow(taskId, fname);
    }
    _pausedPolls.clear();
    // Also do a one-shot refresh so the UI catches up to any state that
    // changed while we were away.
    loadHistory();
  }
});
document.body.addEventListener('dragover', e => {
  // Only react when the user is actually dragging files — ignore text, images, links.
  if (!e.dataTransfer?.types?.includes('Files')) return;
  e.preventDefault();
  if (!document.getElementById('overlay').classList.contains('open')) openModal('file');
});

// ═══════════════════════════════════════════════════════════════
//  BOOT
// ═══════════════════════════════════════════════════════════════
init();
    function loadGaps(filename) {
        if (!filename) return;
        const minSec = parseFloat(document.getElementById('gaps-min-sec')?.value || 1.0);
        const textEl = document.getElementById('gaps-text');
        const countEl = document.getElementById('gaps-count');
        if (!textEl) return;
        textEl.value = 'Carregando...';
        fetch(`/api/gaps/${encodeURIComponent(filename)}?min_gap=${minSec}`)
            .then(r => r.json())
            .then(data => {
                countEl.textContent = data.total_gaps + ' respiro(s) encontrado(s)';
                textEl.value = data.full_text || 'Nenhum texto disponível.';
            })
            .catch(() => { textEl.value = 'Erro ao carregar respiros.'; });
    }

    function downloadGapsTxt() {
        if (!_viewerFile) return;
        const text = document.getElementById('gaps-text').value;
        const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = _viewerFile.name + '_com_respiros.txt';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(a.href);
    }

    async function sendUrl(url, model, language, taskType, filterFillers, folder = '') {
        showToast('Baixando e transcrevendo URL...', '');
        try {
            const fd = new FormData();
            fd.append('url',            url);
            fd.append('model',          model);
            fd.append('language',       language);
            fd.append('task',           taskType);
            fd.append('filter_fillers', filterFillers ? 'true' : 'false');
            fd.append('folder',         folder);
            const r = await fetch('/api/transcribe-url', { method: 'POST', body: fd });
            let data = {};
            try { data = await r.json(); } catch {}
            if (!r.ok) { showToast(data.detail || 'Erro ao processar URL.', 'error'); return; }
            document.getElementById('url-input').value = '';
            await loadHistory();
            if (data.task_id) {
                // Drive progress + toast via the regular per-row poller so URL
                // transcriptions get the same UX as file uploads.
                pollProgressForRow(data.task_id, data.filename || url);
            } else {
                showToast('URL enviada para transcrição!', 'success');
                setTimeout(loadHistory, 1500);
            }
        } catch(e) { showToast('Erro: ' + e.message, 'error'); }
    }

    async function loadMedia() {
      try {
        const res = await fetch('/api/media-history');
        const media = await res.json();
        renderMedia(media);
        _ensureMediaProgressPolling(media);
      } catch { /* ignore */ }
    }

    // For any media row whose status is "processing" (mid-download), look up
    // its task_id via /api/active-tasks and start the standard row poller —
    // the badge will then live-update with "Baixando X%" until done/error/cancelled.
    async function _ensureMediaProgressPolling(mediaList) {
      const active = (mediaList || []).filter(m => m.status === 'processing' || m.status === 'queued');
      if (!active.length) return;
      try {
        const res = await fetch('/api/active-tasks');
        if (!res.ok) return;
        const tasks = await res.json();
        // Index tasks by filename for O(1) lookup
        const byFile = new Map();
        for (const [tid, t] of Object.entries(tasks)) {
          if (t.filename) byFile.set(t.filename, tid);
        }
        for (const m of active) {
          const tid = byFile.get(m.file);
          if (tid && !_activePolls[tid]) pollProgressForRow(tid, m.file);
        }
      } catch { /* ignore */ }
    }

    function renderMedia(data) {
      const tbody = document.getElementById('media-tbody');
      const empty = document.getElementById('empty-media-state');
      const table = tbody.closest('table');
      tbody.innerHTML = '';

      if (!data.length) {
        table.style.display = 'none';
        empty.style.display = 'flex';
        return;
      }
      table.style.display = '';
      empty.style.display = 'none';

      data.forEach(f => {
        const tr = document.createElement('tr');
        tr.dataset.id = f.file;
        const isActive = (f.status === 'processing' || f.status === 'queued');
        // Show cancel option for in-flight downloads; the regular file-management
        // items only make sense for completed media.
        const actionItems = isActive ? `
                <div class="dd-item danger" role="menuitem" tabindex="-1" onclick="cancelMediaDownload('${jsAttr(f.file)}')">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
                  Cancelar download
                </div>` : `
                <div class="dd-item" role="menuitem" tabindex="-1" onclick="dlMediaFile('${jsAttr(f.file)}')">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                  Baixar Original para o PC
                </div>
                <div class="dd-sep"></div>
                <div class="dd-item danger" role="menuitem" tabindex="-1" onclick="deleteMediaFile('${jsAttr(f.file)}')">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>
                  Excluir Mídia Hospedada
                </div>`;
        tr.innerHTML = `
          <td></td>
          <td><div class="file-name">${esc(f.name)}</div></td>
          <td class="col-date"><div class="file-date">${esc(f.date)}</div></td>
          <td class="col-dur"><div class="file-dur">${formatBytes(f.size_bytes)}</div></td>
          <td class="col-status">${renderStatus(f.status, null)}</td>
          <td class="col-actions">
            <div class="action-wrap">
              <button type="button" class="dots-btn" aria-label="Ações" aria-haspopup="menu" aria-expanded="false" onclick="toggleDD('media-${jsAttr(f.id)}',this,event)">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="5" r="1" fill="currentColor"/><circle cx="12" cy="12" r="1" fill="currentColor"/><circle cx="12" cy="19" r="1" fill="currentColor"/></svg>
              </button>
              <div class="dropdown" id="dd-media-${esc(f.id)}" role="menu">${actionItems}
              </div>
            </div>
          </td>`;
        tbody.appendChild(tr);
      });
    }

    // Cancel a download-only or URL→transcribe task via the same /api/transcribe/{tid}
    // endpoint (which is task-type agnostic on the backend).
    async function cancelMediaDownload(filename) {
      closeAllDDs();
      try {
        // Resolve task_id from active tasks (download-only doesn't store task_id in media.json)
        const res = await fetch('/api/active-tasks');
        const tasks = await res.json();
        let taskId = null;
        for (const [tid, t] of Object.entries(tasks)) {
          if (t.filename === filename) { taskId = tid; break; }
        }
        if (!taskId) {
          showToast('Tarefa não está mais ativa (já terminou?).', 'error');
          loadMedia();
          return;
        }
        const ok = await showConfirm({
          title: 'Cancelar download',
          message: 'O download em andamento será interrompido. O arquivo parcial é descartado.',
          confirmText: 'Cancelar download',
          cancelText: 'Continuar',
          danger: true,
        });
        if (!ok) return;
        const r = await fetch(`/api/transcribe/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
        if (!r.ok) { showToast('Não foi possível cancelar.', 'error'); return; }
        _stopPoll(taskId);
        await loadMedia();
        showToast('Download cancelado.', '');
      } catch {
        showToast('Erro de rede ao cancelar.', 'error');
      }
    }

    function dlMediaFile(filename) {
      window.location = `/api/download-media/${encodeURIComponent(filename)}`;
    }

    async function deleteMediaFile(filename) {
      const ok = await showConfirm({
        title: 'Excluir mídia original',
        message: 'O arquivo físico será removido do cofre local e não poderá mais ser re-baixado ou re-transcrito. Espaço em disco será liberado.',
        confirmText: 'Excluir mídia',
        danger: true
      });
      if (!ok) return;
      try {
        const res = await fetch(`/api/delete-media/${encodeURIComponent(filename)}`, { method: 'DELETE' });
        if (!res.ok) {
          let msg = 'Erro ao remover mídia.';
          try { const e = await res.json(); if (e.detail) msg = e.detail; } catch {}
          showToast(msg, 'error');
          return;
        }
        showToast('Mídia original excluída', 'success');
        loadMedia();
      } catch (e) {
        showToast('Erro ao remover', 'error');
      }
    }

    function updateQualityOptions() {
        const typeSelect = document.getElementById('dl-type-select').value;
        const qualSelect = document.getElementById('dl-quality-select');
        qualSelect.innerHTML = '';
        if (typeSelect === 'video') {
            qualSelect.innerHTML = `
              <option value="best">Melhor (Máxima)</option>
              <option value="1080p">1080p</option>
              <option value="720p">720p</option>
              <option value="480p">480p</option>
            `;
        } else {
            qualSelect.innerHTML = `
              <option value="best">Melhor Original</option>
              <option value="worst">Menor Espaço</option>
            `;
        }
    }

    // Track active media-refresh intervals so concurrent downloads don't stack timers.
    const _mediaRefreshIntervals = new Set();
    function _stopMediaRefresh(id) {
        clearInterval(id);
        _mediaRefreshIntervals.delete(id);
    }

    async function startDownloadOnly() {
        const urlVal = document.getElementById('url-input')?.value?.trim();
        const mediaType = document.getElementById('dl-type-select').value;
        const quality = document.getElementById('dl-quality-select').value;

        if (!urlVal) { showToast('Cole uma URL do YouTube válida.', 'error'); return; }
        closeModal();
        showToast(`Baixando apenas o ${mediaType === 'video' ? 'vídeo' : 'áudio'}...`, '');

        try {
            const fd = new FormData();
            fd.append('url', urlVal);
            fd.append('media_type', mediaType);
            fd.append('quality', quality);

            const r = await fetch('/api/yt-download-only', { method: 'POST', body: fd });
            let data = {};
            try { data = await r.json(); } catch {}
            if (!r.ok) { showToast(data.detail || 'Erro ao agendar URL.', 'error'); return; }
            showToast('Mídia adicionada na fila de downloads!', 'success');
            document.getElementById('url-input').value = '';
            switchMainTab('media');
            // NOTE: this is a download-only task — its row lives in the Media tbody,
            // not in the Transcriptions tbody. We intentionally do NOT call
            // pollProgressForRow here (it would try to update a non-existent row in
            // the transcriptions table). Progress is reflected via media list refresh.
            setTimeout(loadMedia, 1500);

            const intervalId = setInterval(loadMedia, 5000);
            _mediaRefreshIntervals.add(intervalId);
            // Auto-stop the refresh after 2 minutes (download usually completes by then).
            setTimeout(() => _stopMediaRefresh(intervalId), 120000);
        } catch(e) { showToast('Erro: ' + e.message, 'error'); }
    }
