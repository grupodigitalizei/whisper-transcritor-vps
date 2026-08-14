// ═══════════════════════════════════════════════════════════════
//  STATE
// ═══════════════════════════════════════════════════════════════
let files    = [];     // [{id, file, name, date, dur, status, mode}]
let selected = new Set();
let pendingFiles = []; // File objects queued from input
let _mediaFiles    = [];         // raw list from /api/media-history (last fetch)
let _mediaSelected = new Set();
const _mediaView = { type: 'all' }; // 'all' | 'audio' | 'video'
let _viewerFile  = null;
let _viewerData  = {};
let _autoSyncInterval = null;
let _syncFingerprint  = '';

// Quem está logado. Preenchido por loadSession() antes de qualquer render, para
// a tela nunca piscar controles de admin para um funcionário.
let _me = { role: null, is_admin: false };

// ═══════════════════════════════════════════════════════════════
//  SESSÃO (admin x funcionário)
// ═══════════════════════════════════════════════════════════════
// O backend já filtra tudo por papel — isto aqui só ajusta a interface. Uma
// falha aqui nunca expõe dado privado, no máximo esconde botões.
async function loadSession() {
  try {
    const r = await fetch('/api/me');
    if (r.status === 401) { window.location.replace('/login'); return; }
    _me = await r.json();
  } catch {
    _me = { role: 'public', is_admin: false };  // fail-closed na UI
  }
  // O CSS usa este atributo para esconder tudo que é .admin-only.
  document.body.dataset.role = _me.is_admin ? 'admin' : 'public';

  const avatar = document.getElementById('session-avatar');
  const role   = document.getElementById('session-role');
  const hint   = document.getElementById('session-hint');
  if (avatar) avatar.textContent = _me.is_admin ? 'AD' : 'EQ';
  if (role)   role.textContent   = _me.is_admin ? 'Administrador' : 'Acesso da equipe';
  if (hint)   hint.textContent   = _me.is_admin ? 'vê tudo' : 'área pública';

  // Funcionário: a tela dele JÁ é a área pública, então a faixa de contexto
  // fica fixa no topo e a aba "Públicas" não existe.
  if (!_me.is_admin) {
    _view.visibility = 'all';   // o servidor já mandou só o que é público
    _syncPublicBanner('public-only');
  }
}

async function doLogout() {
  const ok = await showConfirm({
    title: 'Sair da conta',
    message: 'Você vai precisar digitar a senha de novo para voltar.',
    confirmText: 'Sair',
  });
  if (!ok) return;
  try { await fetch('/api/auth/logout', { method: 'POST' }); } catch { /* segue pro login */ }
  window.location.replace('/login');
}

// ═══════════════════════════════════════════════════════════════
//  INIT
// ═══════════════════════════════════════════════════════════════
async function init() {
  await loadSession();   // define o papel ANTES do primeiro render
  // Lê busca/filtros/ordenação da URL antes de qualquer render, senão a tela
  // pisca o estado padrão e só depois aplica o que o link pedia.
  _hydrateViewFromUrl();
  await Promise.all([loadHistory(), loadStats(), loadFolders()]);
  await resumeActivePolling();
  startAutoSync();
  // Os dois avisos abaixo são de manutenção da máquina (faxina de disco e versão
  // do yt-dlp) e os endpoints são só do admin — nem chamamos como funcionário.
  if (_me.is_admin) {
    // Non-blocking — banner appears later if there are old media files to clean
    checkOldMediaCleanup();
    // Non-blocking — banner appears later if yt-dlp is outdated (may fail silently offline)
    checkYtdlpOutdated();
  }
  // Re-check periodically and whenever the tab regains focus — a tab left
  // open for hours would otherwise show a stale "outdated" banner forever
  // (or keep hiding a real one) since the very first page load.
  if (_me.is_admin) {
    setInterval(checkYtdlpOutdated, 30 * 60 * 1000);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) checkYtdlpOutdated();
    });
  }
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
      // 'public' = está na Área Pública. O backend sempre manda o campo
      // normalizado; o fallback cobre uma resposta antiga em cache.
      visibility:     h.visibility === 'public' ? 'public' : 'private',
      _progress:      carried._progress,
      _phase:         carried._phase,
      _phaseProgress: carried._phaseProgress,
    };
  });
}

function _makeFingerprint(data) {
  return data.map(h => `${h.file}|${h.status}|${h.words}|${h.duration}|${h.visibility || ''}`).join('\n');
}

// Estados de carga da lista: 'loading' | 'ready' | 'error'.
// Governa qual dos 8 estados de tela o usuário vê (skeleton, populado,
// vazio educativo, sem-resultado de busca/filtro, erro com retry).
let _listState = 'loading';

async function loadHistory() {
  const first = _listState === 'loading';
  try {
    const res  = await fetch('/api/history');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    files = _historyToFiles(data);
    _syncFingerprint = _makeFingerprint(data);
    _listState = 'ready';
    renderFiles();
  } catch (e) {
    console.error('loadHistory:', e);
    // Só derruba a tela para o estado de erro se ainda não havia dado nenhum.
    // Se já existe lista na tela, uma falha de sync não pode apagá-la.
    _listState = first ? 'error' : 'ready';
    renderFiles();
  }
}

// Retry do estado de erro — o usuário precisa de um caminho de volta,
// não só de um ícone triste (regra 182).
async function retryLoadHistory() {
  _listState = 'loading';
  renderFiles();
  await loadHistory();
  loadStats?.();
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
  // Na aba pública os números têm que descrever o recorte publicado, não o
  // acervo inteiro. O /api/stats devolve o total do papel, então esse caso é
  // calculado aqui a partir da lista que já está na memória.
  if (_view.visibility === 'public') { _renderLocalStats(); return; }
  try {
    const res  = await fetch('/api/stats');
    const data = await res.json();
    document.getElementById('stat-count').textContent = data.total ?? '0';
    document.getElementById('stat-hours').textContent = data.duration || '0m';
  } catch { /* ignore */ }
}

function _renderLocalStats() {
  const scoped = _filterByVisibility(files);
  const secs   = scoped.reduce((acc, f) => acc + (f.dur_secs || 0), 0);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  document.getElementById('stat-count').textContent = scoped.length;
  document.getElementById('stat-hours').textContent = h ? `${h}h ${m}m` : `${m}m`;
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

// Conta quantos overlays estão abertos, para só liberar o fundo no último.
let _inertDepth = 0;

function _lockBackground() {
  if (_inertDepth++ > 0) return;
  const shell = document.querySelector('.app-shell');
  // `inert` remove o fundo da árvore de acessibilidade E do foco — é o que
  // torna aria-modal="true" verdadeiro. aria-hidden solto não bloqueia Tab.
  if (shell) shell.inert = true;
  // Compensa a largura da barra de rolagem, senão o conteúdo "salta"
  // horizontalmente ao abrir o modal.
  const sw = window.innerWidth - document.documentElement.clientWidth;
  if (sw > 0) document.body.style.paddingRight = sw + 'px';
}

function _unlockBackground() {
  if (--_inertDepth > 0) return;
  _inertDepth = 0;
  const shell = document.querySelector('.app-shell');
  if (shell) shell.inert = false;
  document.body.style.paddingRight = '';
}

function attachFocusTrap(overlayId) {
  const overlay = document.getElementById(overlayId);
  if (!overlay || _activeTraps.has(overlay)) return;
  _lockBackground();
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
  _unlockBackground();
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
    // Ação destrutiva/irreversível usa alertdialog: o leitor de tela anuncia
    // o conteúdo imediatamente em vez de só o título (regra 341).
    overlay.setAttribute('role', type === 'confirm' ? 'alertdialog' : 'dialog');
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
    inp.removeAttribute('aria-invalid');
    inp.setAttribute('aria-describedby', 'dialog-message dialog-error');
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

// Diálogo informativo com um único botão — para avisos que não pedem
// confirmação/cancelamento, só um "ok, entendi" (ex.: "reinicie o app").
function showAlert({ title = 'Aviso', message = '', confirmText = 'OK', iconKind = 'info' } = {}) {
  return _showDialog({
    type: 'alert',
    title,
    iconKind,
    bodyBuilder: ({ msg }) => {
      msg.textContent = message;
      msg.style.display = 'block';
    },
    footBuilder: (foot) => {
      const ok = document.createElement('button');
      ok.className = 'btn btn-primary-lg';
      ok.style.width = '100%';
      ok.textContent = confirmText;
      ok.onclick = () => _dialogClose(true);
      foot.appendChild(ok);
    }
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
            inp.classList.add('invalid');
            inp.setAttribute('aria-invalid', 'true');
            inp.focus();
            return;
          }
        }
        _dialogClose(val);
      };
      inp.onkeydown = (e) => {
        if (e.key === 'Enter') { e.preventDefault(); doConfirm(); }
      };
      // Erro já visível: revalida a cada tecla e some assim que ficar válido.
      inp.oninput = () => {
        if (err.style.display === 'none') return;
        if (!validator || !validator(inp.value)) {
          err.style.display = 'none';
          inp.classList.remove('invalid');
          inp.removeAttribute('aria-invalid');
        } else {
          err.textContent = validator(inp.value);
        }
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
// Na aba "Transcrições Públicas" as contagens do backend (que cobrem o acervo
// inteiro) mentiriam: a pasta apareceria com 26 e a lista com 0. Aqui elas são
// recalculadas a partir da lista já filtrada, incluindo as subpastas.
function _folderCountsForScope() {
  if (_view.visibility !== 'public') return null;
  const scoped = _filterByVisibility(files);
  const counts = new Map();
  for (const f of scoped) {
    const path = f.folder || '';
    if (!path) continue;
    // Propaga para os ancestrais — a contagem de uma pasta inclui as filhas.
    const segs = path.split('/');
    for (let i = 1; i <= segs.length; i++) {
      const anc = segs.slice(0, i).join('/');
      counts.set(anc, (counts.get(anc) || 0) + 1);
    }
  }
  return counts;
}

function _buildFolderTree() {
  const root = { path: '', name: '(raiz)', count: 0, children: {} };
  const scopedCounts = _folderCountsForScope();
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
    node.count = scopedCounts ? (scopedCounts.get(f.path) || 0) : f.count;
  }
  return root;
}

function renderFolderTree() {
  const container = document.getElementById('folder-tree');
  if (!container) return;
  const tree = _buildFolderTree();

  // In-memory counts for the special "virtual" buckets
  const scopedFiles = _filterByVisibility(files);
  const allCount  = scopedFiles.length;
  const rootCount = scopedFiles.filter(f => !(f.folder || '')).length;

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
  document.getElementById('app-sidebar')?.classList.add('open');
  document.getElementById('sidebar-backdrop')?.classList.add('open');
}
function closeSidebar() {
  document.getElementById('app-sidebar')?.classList.remove('open');
  document.getElementById('sidebar-backdrop')?.classList.remove('open');
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
  if (_me.is_admin) _refreshPublicPanel();
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

// ═══════════════════════════════════════════════════════════════
//  ÁREA PÚBLICA — publicar itens e gerenciar o acesso da equipe
// ═══════════════════════════════════════════════════════════════

// Faixa de contexto no topo da tabela. Três estados:
//   'admin-public' → admin na aba Transcrições Públicas
//   'public-only'  → funcionário (a tela dele inteira é a área pública)
//   'none'         → admin na aba normal
function _syncPublicBanner(mode) {
  const box = document.getElementById('public-banner');
  const txt = document.getElementById('public-banner-text');
  if (!box || !txt) return;
  if (mode === 'none') { box.style.display = 'none'; return; }
  box.style.display = 'flex';
  txt.innerHTML = mode === 'public-only'
    ? 'Você está no <strong>acervo compartilhado da equipe</strong>. Tudo que você enviar aqui fica visível para os outros que têm a senha de acesso.'
    : 'Estes são os itens <strong>visíveis para os funcionários</strong>. O resto do seu acervo continua privado. Para publicar, selecione itens na aba <strong>Transcrições</strong> e clique em <strong>Publicar</strong>.';
}

async function _applyVisibility(fileIds, makePublic) {
  const fd = new FormData();
  fd.append('files', fileIds.join('\n'));
  fd.append('visibility', makePublic ? 'public' : 'private');
  const r = await fetch('/api/visibility', { method: 'POST', body: fd });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(d.detail || 'Falha ao mudar a visibilidade.');
  }
  return r.json();
}

// Publica/despublica UM item, pelo menu de três pontinhos.
async function setFileVisibility(id, makePublic) {
  closeAllDDs();
  const f = files.find(x => x.id === id);
  if (makePublic) {
    const ok = await showConfirm({
      title: 'Publicar para os funcionários',
      message: `"${f?.name || id}" passa a aparecer na Área Pública — qualquer pessoa `
             + 'com a senha de acesso vai poder ler a transcrição e baixar o vídeo/áudio.',
      confirmText: 'Publicar',
    });
    if (!ok) return;
  }
  try {
    await _applyVisibility([id], makePublic);
    await loadHistory();
    showToast(makePublic ? 'Publicado na Área Pública.' : 'Item voltou a ser privado.', 'success');
  } catch (e) {
    showToast(e.message, 'error');
  }
}

// Publica/despublica os itens marcados (barra de lote).
async function publishSelected(makePublic) {
  const ids = selectedVisibleIds();
  if (!ids.length) { showToast('Selecione ao menos um item.', 'error'); return; }
  const ok = await showConfirm({
    title: makePublic ? 'Publicar para os funcionários' : 'Tornar privado',
    message: makePublic
      ? `${ids.length} ${ids.length === 1 ? 'item vai' : 'itens vão'} aparecer na Área Pública — `
        + 'quem tem a senha de acesso poderá ler as transcrições e baixar as mídias.'
      : `${ids.length} ${ids.length === 1 ? 'item sai' : 'itens saem'} da Área Pública e volta`
        + `${ids.length === 1 ? '' : 'm'} a ser visível só para você.`,
    confirmText: makePublic ? 'Publicar' : 'Tornar privado',
  });
  if (!ok) return;
  try {
    const res = await _applyVisibility(ids, makePublic);
    selected.clear();
    await loadHistory();
    syncBulkBar();
    showToast(`${res.changed} ${res.changed === 1 ? 'item atualizado' : 'itens atualizados'}.`, 'success');
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function _pubMsg(text, kind) {
  const el = document.getElementById('pub-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'pub-msg' + (text ? ' is-' + kind : '');
}

function _refreshPublicPanel() {
  const publicCount = files.filter(f => f.visibility === 'public').length;
  const sessions    = _me.sessions?.public ?? 0;
  const txt = document.getElementById('pub-stat-text');
  if (txt) {
    txt.textContent =
      `${publicCount} ${publicCount === 1 ? 'transcrição publicada' : 'transcrições publicadas'} · `
      + `${sessions} ${sessions === 1 ? 'funcionário conectado' : 'funcionários conectados'}`;
  }
  const link = document.getElementById('pub-link');
  if (link) link.value = window.location.origin;
  _pubMsg('', '');
}

async function copyPublicLink() {
  const link = document.getElementById('pub-link');
  if (!link) return;
  try {
    await navigator.clipboard.writeText(link.value);
    showToast('Link copiado.', 'success');
  } catch {
    link.select();
    showToast('Copie manualmente (Cmd+C).', 'error');
  }
}

async function changePassword(target) {
  const input   = document.getElementById(target === 'admin' ? 'pub-pw-admin' : 'pub-pw-public');
  const confirm = document.getElementById(target === 'admin' ? 'pub-pw-admin-confirm' : 'pub-pw-public-confirm');
  const pw   = (input?.value || '').trim();
  const pw2  = (confirm?.value || '').trim();
  if (pw.length < 8) {
    _pubMsg('A senha precisa ter pelo menos 8 caracteres.', 'error');
    input?.focus();
    return;
  }
  // Confirmação evita que um typo tranque a equipe fora (o admin reemite a
  // própria sessão, mas os funcionários ficariam com a senha errada).
  if (pw !== pw2) {
    _pubMsg('As duas senhas não são iguais. Repita a mesma senha nos dois campos.', 'error');
    confirm?.focus();
    return;
  }
  const label = target === 'admin' ? 'sua senha de admin' : 'a senha dos funcionários';
  const ok = await showConfirm({
    title: 'Trocar senha',
    message: `Isso muda ${label} e desconecta quem estiver usando esse acesso agora. `
           + 'Guarde a senha nova antes de confirmar — ela não pode ser recuperada depois.',
    confirmText: 'Trocar senha',
    danger: true,
  });
  if (!ok) return;
  try {
    const fd = new FormData();
    fd.append('target', target);
    fd.append('password', pw);
    const r = await fetch('/api/auth/password', { method: 'POST', body: fd });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { _pubMsg(d.detail || 'Não foi possível trocar a senha.', 'error'); return; }
    if (input)   input.value   = '';
    if (confirm) confirm.value = '';
    _pubMsg(target === 'admin'
      ? 'Senha de admin trocada. Você continua logado neste navegador.'
      : 'Senha dos funcionários trocada. Envie a nova para a equipe — as sessões antigas caíram.', 'ok');
    // A contagem de sessões mudou.
    try { _me = await (await fetch('/api/me')).json(); } catch { /* mantém o valor anterior */ }
    _refreshPublicPanel();
  } catch {
    _pubMsg('Erro de rede ao trocar a senha.', 'error');
  }
}

async function revokePublicSessions() {
  const ok = await showConfirm({
    title: 'Desconectar funcionários',
    message: 'Todos os funcionários conectados vão precisar digitar a senha de novo. '
           + 'A senha continua a mesma.',
    confirmText: 'Desconectar todos',
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await fetch('/api/auth/revoke-public', { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { _pubMsg(d.detail || 'Não foi possível desconectar.', 'error'); return; }
    _pubMsg(`${d.revoked} ${d.revoked === 1 ? 'sessão encerrada' : 'sessões encerradas'}.`, 'ok');
    try { _me = await (await fetch('/api/me')).json(); } catch { /* mantém o valor anterior */ }
    _refreshPublicPanel();
  } catch {
    _pubMsg('Erro de rede.', 'error');
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
  const sel = document.getElementById('status-filter-select');
  if (sel) sel.value = status;
  renderFiles();
}

// Filtra por disponibilidade do arquivo original (áudio/vídeo em uploads/) —
// independente do status: dá pra ver, por exemplo, só os erros cujo original
// já foi apagado (não dá mais pra reprocessar sem reenviar).
function setOriginalFilter(kind) {
  _view.originalFilter = kind;
  document.querySelectorAll('.chip[data-original]').forEach(el => {
    el.setAttribute('aria-pressed', el.dataset.original === kind);
  });
  const sel = document.getElementById('original-filter-select');
  if (sel) sel.value = kind;
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

// Contagens dos chips de Status e de Arquivo Original. Cada grupo reflete a
// busca + pasta atual + o filtro do OUTRO grupo (mas nunca o próprio) — é o
// padrão de filtros combináveis: escolher "Erro" não deve zerar a contagem
// de "Disponível", e vice-versa. Antes disso, as contagens usavam a
// biblioteca inteira e ficavam erradas assim que você entrava numa pasta.
function _renderStatusFilterCounts() {
  const folderScoped = _filterByFolder(_filterBySearch(_filterByVisibility(files)));

  const forStatus = _view.originalFilter === 'all' ? folderScoped
    : folderScoped.filter(f => _view.originalFilter === 'available' ? f.has_original : !f.has_original);
  const counts = { all: forStatus.length, queued: 0, processing: 0, done: 0, error: 0 };
  for (const f of forStatus) counts[f.status] = (counts[f.status] || 0) + 1;
  const statusLabels = { all: 'Todos', queued: 'Aguardando', processing: 'Processando', done: 'Concluído', error: 'Erro' };
  for (const k of Object.keys(counts)) {
    const el = document.getElementById('chip-count-' + k);
    if (el) el.textContent = counts[k];
    const opt = document.getElementById('opt-status-' + k);
    if (opt) opt.textContent = `${statusLabels[k]} (${counts[k]})`;
  }

  const forOriginal = _view.statusFilter === 'all' ? folderScoped
    : folderScoped.filter(f => f.status === _view.statusFilter);
  let available = 0, missing = 0;
  for (const f of forOriginal) { if (f.has_original) available++; else missing++; }
  const elAvail = document.getElementById('chip-count-original-available');
  const elMiss  = document.getElementById('chip-count-original-missing');
  if (elAvail) elAvail.textContent = available;
  if (elMiss)  elMiss.textContent  = missing;
  const optAvail = document.getElementById('opt-original-available');
  const optMiss  = document.getElementById('opt-original-missing');
  if (optAvail) optAvail.textContent = `Disponível (${available})`;
  if (optMiss)  optMiss.textContent  = `Apagado (${missing})`;
}

// Sync the mobile "current folder" label + o breadcrumb do header
function _syncFolderButtonLabel() {
  const v = _view.folderFilter;
  const folderName = v === 'all' ? 'Todas'
                   : v === '__root__' ? 'Sem pasta'
                   : v.split('/').slice(-1)[0];
  const el = document.getElementById('current-folder-label');
  if (el) el.textContent = v === 'all' ? 'Pastas' : folderName;
  const bc = document.getElementById('breadcrumb-folder');
  if (bc) bc.textContent = folderName;
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

  const eta  = document.getElementById('queue-summary-eta');
  const fill = document.getElementById('queue-summary-fill');
  if (!box || !text) return;
  if (count === 0) { box.classList.remove('show'); return; }
  box.classList.add('show');

  // Label: contagem de cada grupo — sem repetir "total" duas vezes como antes.
  const parts = [];
  if (inProgress.length) parts.push(`${inProgress.length} em transcrição`);
  if (queued.length) {
    let s = `${queued.length} na fila`;
    if (queuedUnknown > 0) s += ` (${queuedUnknown} sem estimativa)`;
    parts.push(s);
  }
  text.textContent = parts.join(' · ');

  // ETA: um único número, à direita — soma de tudo que falta.
  if (eta) eta.textContent = totalSecs > 0 ? `~${fmtSecs(totalSecs)} restantes` : '';

  // Barra: progresso real do(s) item(ns) em transcrição agora (0 se nada
  // começou ainda, só tem coisa na fila esperando a vaga de concorrência).
  if (fill) {
    let pct = 0;
    if (inProgress.length) {
      const sum = inProgress.reduce((s, f) => {
        const p = (typeof f._phaseProgress === 'number') ? f._phaseProgress
                : (typeof f._progress === 'number')      ? f._progress : 0;
        return s + p;
      }, 0);
      pct = sum / inProgress.length;
    }
    // scaleX sobre trilho de largura fixa, não `width` (regra 137): width
    // dispara layout a cada frame; transform só dispara composite.
    fill.style.transform = `scaleX(${Math.max(0, Math.min(100, pct)) / 100})`;
  }
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

async function promptRenameFile(id) {
  closeAllDDs();
  const f = files.find(x => x.id === id);
  if (!f) return;
  const newName = await showPrompt({
    title: 'Renomear transcrição',
    initialValue: f.name,
    confirmText: 'Renomear',
    iconKind: 'edit',
    validator: (val) => {
      const v = (val || '').trim();
      if (!v) return 'O nome não pode ficar em branco.';
      if (v.length > 150) return 'Máximo de 150 caracteres.';
      if (/[\/\\\x00]/.test(v)) return 'O nome não pode conter /, \\ ou caracteres de controle.';
      return null;
    }
  });
  if (newName === null) return;
  const clean = newName.trim();
  if (!clean || clean === f.name) return;
  try {
    const fd = new FormData();
    fd.append('new_name', clean);
    const res = await fetch(`/api/rename/${encodeURIComponent(f.file)}`, { method: 'POST', body: fd });
    if (!res.ok) {
      let msg = 'Erro ao renomear.';
      try { const e = await res.json(); if (e.detail) msg = e.detail; } catch {}
      showToast(msg, 'error');
      return;
    }
    f.name = clean;
    renderFiles();
    // Se o visualizador estiver aberto para este mesmo arquivo, atualiza o título ali também
    if (_viewerFile && _viewerFile.id === id) {
      _viewerFile.name = clean;
      const titleEl = document.getElementById('viewer-title');
      if (titleEl) titleEl.textContent = clean;
    }
    showToast(`Renomeado para "${clean}".`, 'success');
  } catch {
    showToast('Erro ao renomear.', 'error');
  }
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
// Pipeline: files -> search -> statusFilter -> originalFilter -> folderFilter -> sort -> render
const _view = {
  search:       '',
  // 'all'    → tudo que o servidor mandou (aba Transcrições do admin; e a tela
  //            inteira de um funcionário, que já vem filtrada do servidor)
  // 'public' → só os itens publicados (aba Transcrições Públicas do admin)
  visibility:   'all',
  statusFilter: 'all',      // 'all' | 'queued' | 'processing' | 'done' | 'error'
  originalFilter: 'all',    // 'all' | 'available' | 'missing' — arquivo original ainda no disco?
  folderFilter: 'all',      // 'all' | '__root__' | '<folder name>'
  sortKey:      'date',     // 'name' | 'date' | 'mode' | 'status'
  sortDir:      'desc',     // 'asc' | 'desc'
};

// ═══════════════════════════════════════════════════════════════
//  ESTADO NA URL — busca, filtros e ordenação (regras 433/578)
//  Sem isto o usuário não consegue favoritar uma visão, compartilhar um link
//  filtrado, usar o botão Voltar, e um F5 perdia tudo.
// ═══════════════════════════════════════════════════════════════
// Só grava na URL o que difere do padrão, para o endereço ficar curto e legível.
const _VIEW_URL_DEFAULTS = {
  q: '', status: 'all', orig: 'all', pasta: 'all',
  vis: 'all', ord: 'date', dir: 'desc',
};
const _VIEW_URL_MAP = {
  q:      ['search',         v => v],
  status: ['statusFilter',   v => v],
  orig:   ['originalFilter', v => v],
  pasta:  ['folderFilter',   v => v],
  vis:    ['visibility',     v => v],
  ord:    ['sortKey',        v => v],
  dir:    ['sortDir',        v => v],
};

let _urlSyncTimer = null;

function _syncViewToUrl() {
  // Debounce: digitar na busca não pode gerar uma entrada de histórico por tecla.
  clearTimeout(_urlSyncTimer);
  _urlSyncTimer = setTimeout(() => {
    const params = new URLSearchParams();
    for (const [param, [chave]] of Object.entries(_VIEW_URL_MAP)) {
      const valor = _view[chave];
      if (valor !== undefined && String(valor) !== String(_VIEW_URL_DEFAULTS[param])) {
        params.set(param, String(valor));
      }
    }
    const qs = params.toString();
    const alvo = location.pathname + (qs ? '?' + qs : '') + location.hash;
    // replaceState, não pushState: cada ajuste de filtro não merece uma entrada
    // no histórico — o Voltar deve sair da página, não desfazer filtro por filtro.
    if (alvo !== location.pathname + location.search + location.hash) {
      try { history.replaceState(null, '', alvo); } catch {}
    }
  }, 300);
}

// Lê a URL para dentro de _view. Valida cada valor contra a lista de opções
// aceitas — parâmetro inventado à mão não pode quebrar o render.
const _VIEW_URL_VALIDOS = {
  statusFilter:   ['all', 'queued', 'processing', 'done', 'error'],
  originalFilter: ['all', 'available', 'missing'],
  visibility:     ['all', 'public'],
  sortKey:        ['name', 'date', 'mode', 'status'],
  sortDir:        ['asc', 'desc'],
};

function _hydrateViewFromUrl() {
  let mudou = false;
  const params = new URLSearchParams(location.search);
  for (const [param, [chave]] of Object.entries(_VIEW_URL_MAP)) {
    if (!params.has(param)) continue;
    const bruto = params.get(param);
    const validos = _VIEW_URL_VALIDOS[chave];
    if (validos && !validos.includes(bruto)) continue;   // ignora valor inválido
    _view[chave] = bruto;
    mudou = true;
  }
  if (mudou) {
    // Espelha nos controles visíveis, senão a tela mente sobre o próprio estado.
    const busca = _view.search || '';
    for (const id of ['topbar-search-input', 'search-input']) {
      const el = document.getElementById(id);
      if (el) el.value = busca;
    }
    if (busca) {
      const bar = document.getElementById('search-bar');
      const btn = document.getElementById('search-toggle');
      if (bar) bar.classList.add('open');
      if (btn) { btn.classList.add('active'); btn.setAttribute('aria-expanded', 'true'); }
    }
  }
  return mudou;
}

// Voltar/Avançar do navegador reidrata a visão em vez de não fazer nada.
window.addEventListener('popstate', () => {
  _hydrateViewFromUrl();
  renderFiles();
});

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

// Escopo da aba: na aba "Transcrições Públicas" o admin vê só o que publicou.
// Extraído porque os contadores dos chips e as estatísticas do topo precisam do
// MESMO recorte — senão a aba pública mostraria "1483 arquivos" com 1 linha.
function _filterByVisibility(arr) {
  return _view.visibility === 'public'
    ? arr.filter(f => f.visibility === 'public')
    : arr;
}

// Filtro de busca por nome — extraído pra ser reaproveitado nas contagens dos chips.
function _filterBySearch(arr) {
  const q = _view.search.trim().toLowerCase();
  return q ? arr.filter(f => (f.name || '').toLowerCase().includes(q)) : arr;
}

// Filtro de pasta (item na pasta OU em qualquer subpasta dela) — extraído pra
// ser reaproveitado nas contagens dos chips (senão elas ficam erradas dentro
// de uma pasta, mostrando o total da biblioteca inteira).
function _filterByFolder(arr) {
  if (_view.folderFilter === 'all') return arr;
  if (_view.folderFilter === '__root__') return arr.filter(f => !(f.folder || ''));
  const wanted = _view.folderFilter;
  const prefix = wanted + '/';
  return arr.filter(f => {
    const fp = f.folder || '';
    return fp === wanted || fp.startsWith(prefix);
  });
}

function applyPipeline(allFiles) {
  // Stamp each file with its original array index so we have a stable
  // tiebreaker for date sorting of legacy entries without queued_at.
  const stamped = allFiles.map((f, i) => ({ ...f, _order: i }));
  let arr = _filterBySearch(_filterByVisibility(stamped));
  if (_view.statusFilter !== 'all') arr = arr.filter(f => f.status === _view.statusFilter);
  if (_view.originalFilter !== 'all') {
    arr = arr.filter(f => _view.originalFilter === 'available' ? f.has_original : !f.has_original);
  }
  arr = _filterByFolder(arr);
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
  compress:   'Comprimindo',
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
  // Plano B do download: avisa que o motor padrão falhou e outro está tentando,
  // senão o usuário só veria o progresso voltar a zero sem explicação.
  const engineNote = (f && status === 'processing' && f._engineNote)
    ? `<div class="row-eta">${esc(f._engineNote)}</div>` : '';

  return `<span class="status-badge ${s.cls}" role="img" aria-label="${label}"${extra}>
    <span class="status-dot"></span>${label}${pctLabel}
  </span>${eta}${engineNote}`;
}

// Signature used to decide whether a given row needs DOM work
function _rowSignature(f) {
  return [
    f.id, f.status, f.name, f.date, f.dur, f.mode, f.folder || '',
    f.source || '', f.has_original ? '1' : '0', f.url || '',
    f.visibility || '', selected.has(f.id) ? '1' : '0',
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
        <div class="file-name-row">
          ${f.has_original ? _fileTypeIconHtml(f.file) : ''}
          <div class="file-name">${esc(f.name)}</div>
        </div>
        <div class="file-tags">
          ${f.source === 'url'
            ? `<span class="ftag ftag-url" title="Adicionado via URL (yt-dlp)"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>URL</span>`
            : `<span class="ftag ftag-upload" title="Adicionado por upload"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>Upload</span>`}
          ${(f.visibility === 'public' && _me.is_admin)
            ? `<span class="ftag ftag-public" title="Visível para os funcionários na Área Pública"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>Pública</span>`
            : ''}
          ${f.has_original
            ? `<span class="ftag ftag-orig-ok" title="O arquivo de áudio/vídeo original ainda está em uploads/"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>Original disponível</span>`
            : `<span class="ftag ftag-orig-gone" title="Arquivo original foi apagado — a transcrição continua disponível"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>Original apagado</span>`}
        </div>
      </td>
      <td class="col-date" data-label="Enviado"><div class="file-date">${esc(f.date)}</div></td>
      <td class="col-dur" data-label="Duração"><div class="file-dur">${esc(f.dur)}</div></td>
      <td class="col-mode" data-label="Modelo"><span class="mode-badge">${esc(f.mode)}</span></td>
      <td class="col-status" data-label="Status">${renderStatus(f.status, f)}</td>
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
            </div>` : (f.status === 'error' || f.status === 'cancelled') ? `
            ${f.status === 'error' ? `
            <div class="dd-item danger" role="menuitem" tabindex="-1" onclick="viewError('${jsAttr(f.id)}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
              Ver log de erro
            </div>` : ''}
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="retryFile('${jsAttr(f.id)}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
              Tentar novamente
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
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="dlFile('${jsAttr(f.id)}','md')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="13" y2="17"/></svg>
              Baixar MD (Markdown)
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
            ${_me.is_admin ? (f.visibility === 'public' ? `
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="setFileVisibility('${jsAttr(f.id)}', false)">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
              Tornar privado
            </div>` : `
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="setFileVisibility('${jsAttr(f.id)}', true)">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
              Publicar na Área Pública
            </div>`) + '<div class="dd-sep"></div>' : ''}
            <div class="dd-item" role="menuitem" tabindex="-1" onclick="promptRenameFile('${jsAttr(f.id)}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/></svg>
              Renomear
            </div>
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
  // Espelha a visão na URL (debounced). Ancorado aqui de propósito: é o funil
  // por onde passa toda mudança de busca/filtro/pasta/ordenação.
  if (!dataOverride) _syncViewToUrl();
  const tbody = document.getElementById('files-tbody');
  const empty = document.getElementById('empty-state');
  const table = tbody.closest('table');

  _renderSortIndicators();
  _syncFolderButtonLabel();
  _renderStatusFilterCounts();
  _renderQueueSummary();
  _attachTbodyDelegation(tbody);

  const skel = document.getElementById('files-skeleton');

  // Carregando o primeiro lote: mostra o skeleton (que espelha a estrutura
  // real da tabela) e NADA de "nenhuma transcrição" — seria status falso.
  if (_listState === 'loading' && !files.length) {
    tbody.innerHTML = '';
    _renderedRowSigs.clear();
    table.style.display = 'none';
    empty.style.display = 'none';
    if (skel) skel.classList.add('show');
    return;
  }
  if (skel) skel.classList.remove('show');

  if (!data.length) {
    tbody.innerHTML = '';
    _renderedRowSigs.clear();
    table.style.display = 'none';
    empty.style.display = 'flex';
    _syncEmptyStateCopy();
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
// O estado vazio muda de sentido conforme a aba: na aba pública o problema não
// é "não há transcrição", é "nada foi publicado ainda" — e o botão de enviar
// arquivo ali criaria um item PRIVADO, o que confundiria.
// Há filtro ativo além do padrão? (status, origem, tipo, pasta)
function _hasActiveFilter() {
  return !!(
    (_view.statusFilter   && _view.statusFilter   !== 'all') ||
    (_view.originalFilter && _view.originalFilter !== 'all') ||
    (_view.folderFilter   && _view.folderFilter   !== 'all')
  );
}

// "Nada aqui" colapsa três situações diferentes em uma. Cada uma tem texto
// próprio E ação própria: limpar a busca, limpar os filtros, ou criar o
// primeiro item. Regra 181.
function _syncEmptyStateCopy() {
  const title = document.getElementById('empty-title');
  const sub   = document.getElementById('empty-sub');
  const cta   = document.getElementById('empty-cta');
  if (!title || !sub) return;

  const setCta = (label, handler, show = true) => {
    if (!cta) return;
    cta.style.display = show ? '' : 'none';
    if (!show) return;
    cta.textContent = label;          // remove o ícone do CTA padrão
    cta.onclick     = handler;
  };

  // 1) Falha de carregamento — o que falhou + como tentar de novo.
  if (_listState === 'error' && !files.length) {
    title.textContent = 'Não conseguimos carregar suas transcrições';
    sub.textContent   = 'O servidor não respondeu. Suas transcrições continuam salvas no disco — é só tentar de novo.';
    setCta('Tentar novamente', () => retryLoadHistory());
    return;
  }

  const q = (_view.search || '').trim();

  // 2) Busca sem resultado — repete o termo e mantém um caminho de saída.
  if (q) {
    title.textContent = `Nenhum resultado para “${q}”`;
    sub.textContent   = 'Confira a grafia ou limpe a busca para ver todas as transcrições.';
    setCta('Limpar busca', () => {
      _view.search = '';
      const a = document.getElementById('topbar-search-input');
      const b = document.getElementById('search-input');
      if (a) a.value = ''; if (b) b.value = '';
      renderFiles();
    });
    return;
  }

  // 3) Filtro sem resultado — a ação relaxa o filtro, não manda criar item.
  if (_hasActiveFilter() && files.length) {
    title.textContent = 'Nenhuma transcrição com esses filtros';
    sub.textContent   = 'Existem transcrições, mas nenhuma corresponde à combinação de filtros aplicada.';
    setCta('Limpar filtros', () => {
      _view.statusFilter   = 'all';
      _view.originalFilter = 'all';
      _view.folderFilter   = 'all';
      renderFiles();
    });
    return;
  }

  // 4) Vazio educativo — ensina a função da área e oferece a ação principal.
  const isPublicTab = _view.visibility === 'public';
  if (isPublicTab) {
    title.textContent = 'Nada publicado ainda';
    sub.textContent   = 'Vá em Transcrições, selecione o que a equipe pode ver e clique em Publicar.';
    setCta('', null, false);
  } else {
    title.textContent = 'Nenhuma transcrição ainda';
    sub.textContent   = 'Envie um áudio ou vídeo e a transcrição fica pronta em minutos.';
    setCta('Transcrever primeiro arquivo', () => openModal('file'));
  }
}

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
  if (!cb) return;
  const visible = _getVisibleFiles();
  const visibleSelected = visible.filter(f => selected.has(f.id)).length;
  cb.checked = visibleSelected === visible.length && visible.length > 0;
  cb.indeterminate = visibleSelected > 0 && visibleSelected < visible.length;
  // O rótulo do checkbox tri-state precisa dizer o que o clique VAI fazer.
  cb.setAttribute('aria-label', cb.checked
    ? `Desmarcar as ${visible.length} linhas visíveis`
    : `Selecionar todas as ${visible.length} linhas visíveis`);
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
    _positionDD(dd, btn);
    dd.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
    _ddOpenTrigger = btn;
    // Focus the first menuitem so the menu is usable with the keyboard
    const first = dd.querySelector('[role="menuitem"]');
    if (first) setTimeout(() => first.focus(), 0);
  }
}

// Posiciona o menu de ações como `fixed`, ancorado no botão. Isso o faz
// escapar do `overflow:hidden` do card e do scroll horizontal da tabela —
// senão o menu fica cortado em telas estreitas / linhas na borda.
function _positionDD(dd, btn) {
  const r = btn.getBoundingClientRect();
  const w = dd.offsetWidth  || 192;
  const h = dd.offsetHeight || 240;
  const M = 8;
  let left = r.right - w;                    // alinha a borda direita ao botão
  if (left + w > window.innerWidth - M) left = window.innerWidth - M - w;
  if (left < M) left = M;
  let top = r.bottom + 4;
  if (top + h > window.innerHeight - M) {     // não cabe embaixo → abre pra cima
    const up = r.top - 4 - h;
    top = up >= M ? up : Math.max(M, window.innerHeight - M - h);
  }
  dd.style.position = 'fixed';
  dd.style.right = 'auto';
  dd.style.left = left + 'px';
  dd.style.top  = top + 'px';
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
// Menu de ações é `fixed`: fecha ao rolar (que não seja dentro dele) ou redimensionar
document.addEventListener('scroll', e => {
  if (e.target && e.target.closest && e.target.closest('.dropdown')) return;
  closeAllDDs();
}, true);
window.addEventListener('resize', () => closeAllDDs());
// ESC fecha UMA camada por pressionamento, da mais interna para a mais
// externa, devolvendo o foco ao gatilho correspondente (regra 393).
// Antes fechava tudo de uma vez, o que destruía o contexto do usuário.
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;

  const isOpen = id => {
    const el = document.getElementById(id);
    return el && el.classList.contains('open');
  };

  // 1. Menus/dropdowns e custom selects são sempre a camada mais interna.
  const openCs = document.querySelector('.cs.cs-open');
  if (openCs) { openCs._csClose && openCs._csClose(); return; }
  if (document.querySelector('.dropdown.open')) { closeAllDDs(true); return; }
  // 2. Diálogos empilhados sobre um modal.
  if (isOpen('dialog-overlay'))        { _dialogCancel();      return; }
  if (isOpen('folder-picker-overlay')) { closeFolderPicker();  return; }
  // 3. Modais.
  if (isOpen('settings-overlay'))      { closeSettings();      return; }
  if (isOpen('viewer-overlay'))        { closeViewer();        return; }
  if (isOpen('overlay'))               { closeModal();         return; }
  // 4. Drawer da sidebar no mobile.
  closeSidebar();
});

// Cmd/Ctrl+A no escopo da lista seleciona todas as linhas VISÍVEIS (não o
// banco inteiro), e ESC limpa a seleção quando não há overlay aberto.
// Regra 327. Atalho desativado enquanto o foco está em campo de texto.
document.addEventListener('keydown', e => {
  const el = document.activeElement;
  const typing = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
                        || el.tagName === 'SELECT' || el.isContentEditable);
  if (typing) return;
  if (document.querySelector('.overlay.open')) return;

  if ((e.metaKey || e.ctrlKey) && (e.key === 'a' || e.key === 'A')) {
    const visible = _getVisibleFiles();
    if (!visible.length) return;
    e.preventDefault();
    visible.forEach(f => selected.add(f.id));
    renderFiles(); syncBulkBar(); syncHeaderCheck();
    showToast(`${visible.length} selecionado${visible.length !== 1 ? 's' : ''}`, '');
    return;
  }
  if (e.key === 'Escape' && selected.size) {
    selected.clear();
    renderFiles(); syncBulkBar(); syncHeaderCheck();
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
  // Mesmo tratamento de foco do viewer normal: sem isto o Tab continuava
  // percorrendo a página por trás do overlay e o foco nunca voltava.
  attachFocusTrap('viewer-overlay');
  setTimeout(() => document.querySelector('#viewer-overlay .modal-close')?.focus(), 50);
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

// Ícones específicos pra cada escopo de exclusão (só transcrição / só mídia / ambos)
const _DELETE_SCOPE_ICONS = {
  transcription: `<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>`,
  media:         `<polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>`,
  both:          _DIALOG_ICONS.danger,
};

// Pergunta o que excluir: só a transcrição (mantém o original na Biblioteca),
// só o arquivo original (mantém a transcrição, libera espaço), ou os dois.
// Retorna null se o usuário cancelar.
async function _promptDeleteScope(message) {
  return showChoice({
    title: 'Excluir o quê?',
    message,
    cancelText: 'Cancelar',
    choices: [
      { value: 'transcription', label: 'Só a transcrição', icon: _DELETE_SCOPE_ICONS.transcription,
        description: 'Remove TXT/SRT/JSON/MD do histórico — o arquivo original continua na Biblioteca de Mídia' },
      { value: 'media', label: 'Só o arquivo original', icon: _DELETE_SCOPE_ICONS.media,
        description: 'Libera espaço em disco — a transcrição continua disponível pra consultar e baixar' },
      { value: 'both', label: 'Os dois', icon: _DELETE_SCOPE_ICONS.both, danger: true,
        description: 'Remove tudo — transcrição e arquivo original' },
    ],
  });
}

async function deleteFile(id) {
  closeAllDDs();
  const f = files.find(x => x.id === id);
  if (!f) return;
  const scope = await _promptDeleteScope(`"${f.name}" — escolha o que remover:`);
  if (!scope) return;
  try {
    const res = await fetch(`/api/delete/${encodeURIComponent(f.file)}?scope=${scope}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('delete failed');
    if (scope === 'media') {
      // A transcrição continua — só atualiza o badge de original localmente.
      f.has_original = false;
      renderFiles();
    } else {
      // 'transcription' ou 'both': o item some da lista de Transcrições.
      files.splice(files.findIndex(x => x.id === id), 1);
      selected.delete(id);
      renderFiles();
      syncBulkBar();
    }
    await loadStats();
    showToast(
      scope === 'media' ? 'Arquivo original excluído — transcrição mantida.' :
      scope === 'transcription' ? 'Transcrição excluída — arquivo original mantido.' :
      'Excluído (transcrição e arquivo original).',
      'success');
  } catch {
    showToast('Erro ao excluir.', 'error');
  }
}

// ═══════════════════════════════════════════════════════════════
//  TENTAR NOVAMENTE — refaz manualmente um item com erro/cancelado.
//  O backend decide sozinho o que refazer (transcrição, download, ou os
//  dois) olhando o que foi pedido originalmente para aquele arquivo.
// ═══════════════════════════════════════════════════════════════
async function retryFile(id) {
  closeAllDDs();
  const f = files.find(x => x.id === id);
  if (!f) return;
  try {
    const res = await fetch(`/api/retry/${encodeURIComponent(f.file)}`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { showToast(data.detail || 'Erro ao tentar novamente.', 'error'); return; }
    showToast('Reenviado — acompanhe o progresso na lista.', 'success');
    await loadHistory();
  } catch {
    showToast('Erro de rede ao tentar novamente.', 'error');
  }
}

async function retrySelected() {
  const ids = selectedVisibleIds();
  if (!ids.length) return;
  const retryable = ids
    .map(id => files.find(x => x.id === id))
    .filter(f => f && (f.status === 'error' || f.status === 'cancelled'));
  if (!retryable.length) {
    showToast('Nenhum selecionado está com erro ou cancelado.', 'error');
    return;
  }
  const skipped = ids.length - retryable.length;
  const ok = await showConfirm({
    title: `Tentar novamente ${retryable.length} ${retryable.length === 1 ? 'item' : 'itens'}?`,
    message: 'Refaz com a mesma configuração original — transcrição, download, ou os dois, ' +
      'dependendo do que foi pedido no início.' +
      (skipped ? ` ${skipped} selecionado${skipped === 1 ? '' : 's'} não está${skipped === 1 ? '' : 'ão'} ` +
                 `com erro e será${skipped === 1 ? '' : 'ão'} ignorado${skipped === 1 ? '' : 's'}.` : ''),
    confirmText: 'Tentar novamente',
  });
  if (!ok) return;
  try {
    const fd = new FormData();
    fd.append('files', JSON.stringify(retryable.map(f => f.file)));
    const res = await fetch('/api/retry-batch', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { showToast(data.detail || 'Erro ao tentar novamente.', 'error'); return; }
    showToast(
      `${data.submitted}/${data.total} reenviado(s)` +
      (data.failed ? `, ${data.failed} falhou(aram).` : '.'),
      'success');
    selected.clear();
    await loadHistory();
    syncBulkBar();
  } catch {
    showToast('Erro de rede ao tentar novamente.', 'error');
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
      { value: 'md',         label: 'MD',           description: 'Markdown com título e metadados', defaultChecked: false },
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
  const scope = await _promptDeleteScope(
    `${count} item${count === 1 ? '' : 's'} selecionado${count === 1 ? '' : 's'} — escolha o que remover:`);
  if (!scope) return;
  let failed = 0;
  for (const id of ids) {
    const f = files.find(x => x.id === id);
    if (f) {
      try {
        const res = await fetch(`/api/delete/${encodeURIComponent(f.file)}?scope=${scope}`, { method: 'DELETE' });
        if (!res.ok) { failed++; continue; }
        if (scope === 'media') {
          f.has_original = false;
        } else {
          files.splice(files.findIndex(x => x.id === id), 1);
          selected.delete(id);
        }
      } catch { failed++; }
    }
  }
  renderFiles();
  syncBulkBar();
  await loadStats();
  const verb = scope === 'media' ? 'liberado(s)' : 'excluído(s)';
  if (failed === 0) {
    showToast(`${count} arquivo(s) ${verb}.`, 'success');
  } else {
    showToast(`${count - failed} ${verb}, ${failed} falha(s).`, 'error');
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
    + paths.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('')
    + '<option value="__new__">➕ Criar nova pasta…</option>';
  // Restore previous choice, else default to the folder currently in view.
  // "__new__" is preserved too so re-populating (e.g. on folder reload) doesn't
  // kick the user out of the create-folder flow they just started.
  sel.value = (prev === '__new__' || paths.includes(prev))
    ? prev
    : (paths.includes(activeFolder) ? activeFolder : '');
  onFolderSelectChange();

  // Belt-and-suspenders: if the folder list hasn't loaded yet (modal opened
  // before init finished), fetch it and re-populate once it arrives.
  if (!paths.length) {
    loadFolders().then(() => {
      if ((_folders || []).length) _populateFolderSelect();
    });
  }
}

// Show/hide the "new folder" text field based on the dropdown selection.
// The field only appears when the user picks "➕ Criar nova pasta…".
function onFolderSelectChange() {
  const sel  = document.getElementById('folder-select');
  const wrap = document.getElementById('new-folder-wrap');
  if (!sel || !wrap) return;
  const creating = sel.value === '__new__';
  wrap.style.display = creating ? '' : 'none';
  if (creating) {
    const inp = document.getElementById('new-folder-input');
    if (inp) setTimeout(() => inp.focus(), 30);
  }
}

// Live client-side check of the typed folder name. Mirrors the backend rules in
// _validate_folder_name (segments 1-60 chars, no '\', no control/null, no '.'/'..',
// no empty/double-slash segments). Returns '' if invalid, else the canonical path.
// Shows an inline hint so the user gets feedback before submitting.
function _canonicalNewFolder(raw) {
  const folder = (raw || '').trim().replace(/^\/+|\/+$/g, '');
  if (!folder) return { ok: false, path: '', error: '' };
  if (folder.includes('\\') || /[\x00-\x1f]/.test(folder)) {
    return { ok: false, path: '', error: 'Caractere inválido (barra invertida ou controle).' };
  }
  const segs = folder.split('/');
  for (const s of segs) {
    const seg = s.trim();
    if (!seg) return { ok: false, path: '', error: 'Segmento vazio (barras duplicadas?).' };
    if (seg === '.' || seg === '..') return { ok: false, path: '', error: `Segmento inválido: "${seg}".` };
    if (seg.length > 60) return { ok: false, path: '', error: 'Cada parte deve ter no máximo 60 caracteres.' };
  }
  return { ok: true, path: segs.map(s => s.trim()).join('/'), error: '' };
}

function validateNewFolderInput() {
  const inp  = document.getElementById('new-folder-input');
  const hint = document.getElementById('new-folder-hint');
  if (!inp || !hint) return;
  const raw = inp.value;
  if (!raw.trim()) {
    hint.textContent = 'Use / para criar subpastas. A pasta será criada ao transcrever.';
    hint.style.color = 'var(--slate-600)';
    return;
  }
  const res = _canonicalNewFolder(raw);
  if (res.ok) {
    const exists = (_folders || []).some(f => f.path === res.path);
    hint.textContent = exists
      ? `A pasta "${res.path}" já existe — os itens serão salvos nela.`
      : `Será criada a pasta "${res.path}".`;
    hint.style.color = 'var(--slate-600)';
  } else {
    hint.textContent = res.error;
    hint.style.color = 'var(--red-600, #dc2626)';
  }
}

// Resolve the destination folder chosen in the upload modal. When the user opted
// to create a new folder, validate + return the typed path (the backend creates
// the tree on transcribe). Returns { ok, folder, error } — ok=false blocks submit.
function resolveModalFolder() {
  const sel = document.getElementById('folder-select');
  const value = sel?.value || '';
  if (value !== '__new__') return { ok: true, folder: value, error: '' };
  const raw = document.getElementById('new-folder-input')?.value || '';
  if (!raw.trim()) return { ok: false, folder: '', error: 'Digite o nome da nova pasta.' };
  const res = _canonicalNewFolder(raw);
  if (!res.ok) return { ok: false, folder: '', error: res.error || 'Nome de pasta inválido.' };
  return { ok: true, folder: res.path, error: '' };
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
  main:  ['main-tab-transcriptions', 'main-tab-public', 'main-tab-media',
          'main-tab-social', 'main-tab-subs', 'main-tab-advanced'],
  modal: ['tab-file', 'tab-url', 'tab-batch'],
  'adv-type':    ['adv-type-video', 'adv-type-audio'],
  'adv-urlmode': ['adv-urlmode-single', 'adv-urlmode-batch'],
};
function onTablistKey(e, group) {
  // Abas escondidas (ex.: "Transcrições Públicas" para um funcionário) saem da
  // rotação — as setas nunca param num botão invisível.
  const ids = (_TABLIST_GROUPS[group] || []).filter(id => {
    const el = document.getElementById(id);
    return el && el.offsetParent !== null;
  });
  if (!ids.length) return;
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

const _MAIN_TAB_TITLES = {
  transcriptions: 'Transcrições',
  public:         'Transcrições Públicas',
  media:          'Biblioteca de Mídia',
  social:         'Redes Sociais',
  subs:           'Assinaturas',
  advanced:       'Download Avançado',
};

// "public" não tem card próprio: é a MESMA tabela de transcrições com o escopo
// trocado. Reusar em vez de duplicar mantém as duas abas com exatamente as
// mesmas funções (filtros, lote, renomear, respiros, ZIP...) para sempre.
const _MAIN_TAB_CARDS = {
  transcriptions: 'card-transcriptions',
  public:         'card-transcriptions',
  media:          'card-media',
  social:         'card-social',
  subs:           'card-subs',
  advanced:       'card-advanced',
};

// Abas restritas ao admin. A equipe não tem o botão, mas quem chama
// switchMainTab por outro caminho cai aqui e volta para a aba inicial.
const _ADMIN_ONLY_TABS = new Set(['public', 'social', 'subs']);

function switchMainTab(tab) {
  if (_ADMIN_ONLY_TABS.has(tab) && !_me.is_admin) tab = 'transcriptions';
  const activeCard = _MAIN_TAB_CARDS[tab] || 'card-transcriptions';
  ['transcriptions', 'public', 'media', 'social', 'advanced'].forEach(t => {
    const pressed = t === tab;
    const btn = document.getElementById('main-tab-' + t);
    if (btn) {
      btn.setAttribute('aria-selected', pressed);
      btn.setAttribute('aria-pressed', pressed); // legacy — kept for any callers reading it
      btn.tabIndex = pressed ? 0 : -1;
    }
    // Título do header acompanha a seção ativa
    if (pressed) {
      const title = document.getElementById('page-title');
      if (title) title.textContent = _MAIN_TAB_TITLES[t];
    }
    // Use '' to clear the inline style so the element falls back to its CSS
    // rule. #card-transcriptions is .card-with-sidebar (display:grid on desktop,
    // block on mobile via media query); forcing 'block' would break the grid.
    const card = document.getElementById(_MAIN_TAB_CARDS[t]);
    if (card) card.style.display = (_MAIN_TAB_CARDS[t] === activeCard) ? '' : 'none';
  });

  // Troca o escopo e repinta. Um funcionário fica sempre em 'all' — o servidor
  // já mandou apenas o acervo público para ele.
  if (_me.is_admin && (tab === 'transcriptions' || tab === 'public')) {
    _view.visibility = (tab === 'public') ? 'public' : 'all';
    selected.clear();          // seleção de uma aba não vaza para a outra
    _syncPublicBanner(tab === 'public' ? 'admin-public' : 'none');
    renderFiles();
    syncBulkBar();
    _renderStatusFilterCounts();
    renderFolderTree();
    loadStats();
  }

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
  // Redes Sociais é só do admin (o botão está escondido para a equipe, mas o
  // guard aqui evita que uma navegação por teclado ou um link antigo dispare as
  // chamadas — que o servidor recusaria com 403 e sujariam a tela de erro).
  if (tab === 'social' && _me.is_admin && typeof initSocialTab === 'function') initSocialTab();
  // Assinaturas: idem — o botão está escondido para a equipe, e o guard evita
  // disparar chamadas que o servidor recusaria com 403.
  if (tab === 'subs' && _me.is_admin && typeof loadSubscriptions === 'function') loadSubscriptions();
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
//  MEDIA HOVER PREVIEW — ícone de tipo na linha; passar o mouse mostra o
//  primeiro frame real do vídeo (capturado via <video>+<canvas>) ou um
//  ícone de áudio para arquivos sem representação visual possível.
// ═══════════════════════════════════════════════════════════════
const _VIDEO_EXTS_JS = ['mp4','mov','mkv','avi','webm','wmv','mpeg','mpg','m4v'];
const _AUDIO_EXTS_JS = ['mp3','m4a','aac','wav','ogg','opus','wma','flac'];

function _fileTypeFor(filename) {
  const ext = (filename || '').split('.').pop().toLowerCase();
  if (_VIDEO_EXTS_JS.includes(ext)) return 'video';
  if (_AUDIO_EXTS_JS.includes(ext)) return 'audio';
  return 'other';
}

function _mediaTypeIconSvg(type) {
  return type === 'video'
    ? '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>'
    : '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>';
}

// Markup do botão-ícone inserido na linha. Só é gerado quando o arquivo
// original ainda está no disco (sem original não há nada pra pré-visualizar).
function _fileTypeIconHtml(filename) {
  const type = _fileTypeFor(filename);
  if (type === 'other') return '';
  const label = type === 'video' ? 'Pré-visualizar vídeo (passe o mouse)' : 'Arquivo de áudio';
  return `<button type="button" class="file-type-icon" data-file="${jsAttr(filename)}" data-type="${type}"
            aria-label="${label}" title="${label}"
            onmouseenter="showMediaPreview(this)" onmouseleave="hideMediaPreview()"
            onfocus="showMediaPreview(this)" onblur="hideMediaPreview()">
            ${_mediaTypeIconSvg(type)}
          </button>`;
}

const _videoThumbCache = new Map(); // file -> dataURL | 'error'
let _previewHoverTimer = null;
let _previewHideTimer  = null;
let _previewToken = 0; // incrementado a cada hide — invalida gerações em andamento

function _ensurePreviewPanel() {
  let el = document.getElementById('media-hover-preview');
  if (!el) {
    el = document.createElement('div');
    el.id = 'media-hover-preview';
    el.className = 'media-hover-preview';
    el.setAttribute('role', 'tooltip');
    document.body.appendChild(el);
  }
  return el;
}

function _positionPreviewPanel(panel, btn) {
  const r = btn.getBoundingClientRect();
  const w = panel.offsetWidth  || 200;
  const h = panel.offsetHeight || 120;
  const M = 10;
  let left = r.left;
  if (left + w > window.innerWidth - M) left = window.innerWidth - M - w;
  if (left < M) left = M;
  let top = r.bottom + 8;
  if (top + h > window.innerHeight - M) top = r.top - 8 - h;
  panel.style.left = left + 'px';
  panel.style.top  = top + 'px';
}

function showMediaPreview(btn) {
  clearTimeout(_previewHideTimer);
  clearTimeout(_previewHoverTimer);
  // Pequeno atraso — passar o mouse rapidamente por várias linhas não deve
  // disparar uma captura de vídeo pra cada uma.
  _previewHoverTimer = setTimeout(() => _renderPreview(btn, btn.dataset.file, btn.dataset.type), 150);
}

function hideMediaPreview() {
  clearTimeout(_previewHoverTimer);
  _previewToken++; // qualquer captura de frame em andamento vira descartável
  const el = document.getElementById('media-hover-preview');
  if (!el) return;
  clearTimeout(_previewHideTimer);
  _previewHideTimer = setTimeout(() => el.classList.remove('show'), 80);
}

async function _renderPreview(btn, file, type) {
  const panel = _ensurePreviewPanel();
  const myToken = ++_previewToken;

  if (type === 'audio') {
    panel.innerHTML = `<div class="mhp-audio">${_mediaTypeIconSvg('audio')}<span>Arquivo de áudio</span></div>`;
    panel.classList.add('show');
    _positionPreviewPanel(panel, btn);
    return;
  }

  const cached = _videoThumbCache.get(file);
  if (cached) {
    panel.innerHTML = cached === 'error'
      ? `<div class="mhp-audio">${_mediaTypeIconSvg('video')}<span>Prévia indisponível</span></div>`
      : `<img src="${cached}" alt="Prévia do primeiro frame do vídeo" class="mhp-thumb" />`;
    panel.classList.add('show');
    _positionPreviewPanel(panel, btn);
    return;
  }

  panel.innerHTML = `<div class="mhp-loading">Gerando prévia…</div>`;
  panel.classList.add('show');
  _positionPreviewPanel(panel, btn);

  try {
    const dataUrl = await _captureVideoFrame(file);
    _videoThumbCache.set(file, dataUrl);
    if (myToken !== _previewToken) return; // mouse já saiu — não troca mais nada na tela
    panel.innerHTML = `<img src="${dataUrl}" alt="Prévia do primeiro frame do vídeo" class="mhp-thumb" />`;
    _positionPreviewPanel(panel, btn);
  } catch {
    _videoThumbCache.set(file, 'error');
    if (myToken !== _previewToken) return;
    panel.innerHTML = `<div class="mhp-audio">${_mediaTypeIconSvg('video')}<span>Prévia indisponível</span></div>`;
    _positionPreviewPanel(panel, btn);
  }
}

// Baixa (via range request do <video>, não o arquivo inteiro) só o suficiente
// pra decodificar o primeiro frame e desenhá-lo num canvas.
function _captureVideoFrame(file) {
  return new Promise((resolve, reject) => {
    const video = document.createElement('video');
    video.muted = true;
    video.playsInline = true;
    video.preload = 'metadata';
    video.src = `/api/download-media/${encodeURIComponent(file)}`;

    let settled = false;
    const cleanup = () => { video.removeAttribute('src'); video.load(); };
    const fail = () => { if (settled) return; settled = true; cleanup(); reject(new Error('preview failed')); };
    const done = (dataUrl) => { if (settled) return; settled = true; cleanup(); resolve(dataUrl); };

    video.addEventListener('error', fail, { once: true });
    video.addEventListener('loadedmetadata', () => {
      // ~0.1s em vez de 0 exato — em alguns codecs o frame 0 renderiza preto
      video.currentTime = Math.min(0.1, (video.duration || 1) / 2);
    }, { once: true });
    video.addEventListener('seeked', () => {
      try {
        const targetW = 240;
        const scale   = targetW / (video.videoWidth || targetW);
        const canvas  = document.createElement('canvas');
        canvas.width  = targetW;
        canvas.height = Math.round((video.videoHeight || 135) * scale);
        canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
        done(canvas.toDataURL('image/jpeg', 0.72));
      } catch {
        fail();
      }
    }, { once: true });

    setTimeout(fail, 8000); // trava de segurança se o vídeo nunca carregar
  });
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

// Chave estável para deduplicar (mesmo arquivo arrastado duas vezes não deve
// entrar duplicado na lista). File objects não têm id — nome+tamanho+data de
// modificação já é suficiente na prática.
function _pendingFileKey(f) {
  return `${f.name}::${f.size}::${f.lastModified}`;
}

// Cada seleção/drop ADICIONA à lista de pendentes em vez de substituir — o
// dropzone continua visível (não é mais escondido) para receber mais arquivos.
function handleFileSelect(fileList) {
  if (!fileList || !fileList.length) return;
  const existingKeys = new Set(pendingFiles.map(_pendingFileKey));
  let added = 0, skipped = 0;
  for (const f of Array.from(fileList)) {
    const key = _pendingFileKey(f);
    if (existingKeys.has(key)) { skipped++; continue; }
    existingKeys.add(key);
    pendingFiles.push(f);
    added++;
  }
  renderPendingFiles();
  // Limpa o <input> nativo — sem isso, selecionar o MESMO arquivo de novo não
  // dispara 'change' (o browser não refire quando a FileList parece idêntica).
  document.getElementById('file-input').value = '';
  if (skipped) {
    showToast(added ? `${added} adicionado(s), ${skipped} já estava(m) na lista.` : 'Arquivo já estava na lista.', '');
  }
}

function removePendingFile(i) {
  pendingFiles.splice(i, 1);
  renderPendingFiles();
}

function clearPendingFiles() {
  pendingFiles = [];
  document.getElementById('file-input').value = '';
  renderPendingFiles();
}

function renderPendingFiles() {
  const wrap = document.getElementById('file-list-wrap');
  const list = document.getElementById('file-list');
  const summary = document.getElementById('file-list-summary');
  if (!pendingFiles.length) {
    wrap.classList.remove('show');
    list.innerHTML = '';
    return;
  }
  wrap.classList.add('show');
  const totalBytes = pendingFiles.reduce((s, x) => s + x.size, 0);
  summary.textContent =
    `${pendingFiles.length} arquivo${pendingFiles.length !== 1 ? 's' : ''} selecionado${pendingFiles.length !== 1 ? 's' : ''} · ${formatBytes(totalBytes)}`;
  list.innerHTML = pendingFiles.map((f, i) => `
    <div class="file-list-item">
      <div class="file-chip-icon" aria-hidden="true">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
      </div>
      <div class="file-chip-info">
        <div class="file-chip-name">${esc(f.name)}</div>
        <div class="file-chip-size">${formatBytes(f.size)}</div>
      </div>
      <button type="button" class="file-chip-remove" onclick="removePendingFile(${i})" aria-label="Remover ${esc(f.name)}">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>`).join('');
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
//  DOWNLOAD AVANÇADO — aba dedicada (sidebar): áudio/vídeo, playlist inteira,
//  legendas, metadados e thumbnail embutidos, faixa de áudio por idioma.
// ═══════════════════════════════════════════════════════════════
function setAdvType(type) {
  ['video', 'audio'].forEach(t => {
    const btn = document.getElementById('adv-type-' + t);
    btn.setAttribute('aria-selected', t === type);
    btn.tabIndex = t === type ? 0 : -1;
  });
  // Qualidade não se aplica do mesmo jeito a áudio — troca as opções do select
  const q = document.getElementById('adv-quality-select');
  if (q) {
    q.innerHTML = type === 'video'
      ? `<option value="best">Melhor (Máxima)</option>
         <option value="1080p">1080p</option>
         <option value="720p">720p</option>
         <option value="480p">480p</option>`
      : `<option value="best">Melhor Original</option>
         <option value="worst">Menor Espaço</option>`;
  }
  // Formato de saída: cada tipo mostra só o seu seletor (container de vídeo
  // não significa nada para áudio, e vice-versa).
  const contWrap  = document.getElementById('adv-container-wrap');
  const audioWrap = document.getElementById('adv-audioformat-wrap');
  if (contWrap)  contWrap.style.display  = type === 'video' ? '' : 'none';
  if (audioWrap) audioWrap.style.display = type === 'audio' ? '' : 'none';

  // Legendas só existem embutidas em vídeo — em áudio não há onde colocá-las,
  // então desliga e trava o toggle pra não gerar um .srt órfão no disco.
  const subsToggle = document.getElementById('adv-subs-toggle');
  const subsRow    = subsToggle?.closest('.toggle-row');
  if (subsToggle) {
    subsToggle.disabled = type === 'audio';
    if (type === 'audio' && subsToggle.checked) {
      subsToggle.checked = false;
      _toggleAdvSubOptions();
    }
    subsRow?.classList.toggle('toggle-row-disabled', type === 'audio');
    const sub = subsRow?.querySelector('.toggle-sub');
    if (sub) {
      sub.textContent = type === 'audio'
        ? 'Só disponível para vídeo — áudio não tem onde embutir a legenda'
        : 'Embutidas no arquivo (.srt) — quando o vídeo tiver legendas disponíveis';
    }
  }
}

function _toggleAdvSubOptions() {
  const on = document.getElementById('adv-subs-toggle').checked;
  document.getElementById('adv-subs-options').style.display = on ? 'block' : 'none';
}

function setAdvUrlMode(mode) {
  ['single', 'batch'].forEach(m => {
    const btn = document.getElementById('adv-urlmode-' + m);
    btn.setAttribute('aria-selected', m === mode);
    btn.tabIndex = m === mode ? 0 : -1;
  });
  document.getElementById('adv-url-single-wrap').style.display = mode === 'single' ? '' : 'none';
  document.getElementById('adv-url-batch-wrap').style.display  = mode === 'batch'  ? '' : 'none';
  const sub = document.getElementById('adv-playlist-sub');
  if (sub) {
    sub.textContent = mode === 'batch'
      ? 'Cada link colado que for playlist/canal também é expandido — baixa cada vídeo (até 100 por link), um item por vídeo na Biblioteca'
      : 'A URL é de uma playlist/canal — baixa cada vídeo (até 100), um item por vídeo na Biblioteca';
  }
}

function _updateAdvBatchCount() {
  const urls = _extractUrlsFromText(document.getElementById('adv-urls-batch').value);
  const summary = document.getElementById('adv-batch-summary');
  const count   = document.getElementById('adv-batch-count');
  if (!urls.length) { summary.style.display = 'none'; return; }
  summary.style.display = 'block';
  count.textContent = urls.length;
}

async function submitAdvancedDownload() {
  const isBatch = document.getElementById('adv-urlmode-batch').getAttribute('aria-selected') === 'true';

  let singleUrl = '', batchUrls = [];
  if (isBatch) {
    batchUrls = _extractUrlsFromText(document.getElementById('adv-urls-batch').value);
    if (!batchUrls.length) { showToast('Cole ao menos uma URL (uma por linha).', 'error'); return; }
  } else {
    singleUrl = document.getElementById('adv-url-input').value.trim();
    if (!singleUrl) { showToast('Cole uma URL primeiro.', 'error'); return; }
    if (!/^https?:\/\//i.test(singleUrl)) { showToast('URL inválida — use http:// ou https://', 'error'); return; }
  }

  const mediaType   = document.getElementById('adv-type-video').getAttribute('aria-selected') === 'true' ? 'video' : 'audio';
  const quality     = document.getElementById('adv-quality-select').value;
  const audioLang   = document.getElementById('adv-audio-lang').value.trim();
  const isPlaylist  = document.getElementById('adv-playlist-toggle').checked;
  const subtitles   = document.getElementById('adv-subs-toggle').checked;
  const subLangs    = document.getElementById('adv-sub-langs').value.trim() || 'pt,en';
  const autoSubs    = document.getElementById('adv-auto-subs').checked;
  const metadata    = document.getElementById('adv-metadata-toggle').checked;
  const thumbnail   = document.getElementById('adv-thumb-toggle').checked;

  const btn = document.getElementById('adv-download-btn');

  if (!isBatch && isPlaylist) {
    // Um único link: resolve primeiro pra mostrar quantos vídeos foram
    // encontrados e pedir confirmação antes de disparar tudo de uma vez.
    // (No modo lote isso exigiria 1 resolve por link — o backend já aplica
    // um teto de segurança total, então a confirmação abaixo cobre esse caso.)
    btn.disabled = true;
    let info;
    try {
      const r = await fetch(`/api/resolve-playlist?url=${encodeURIComponent(singleUrl)}`);
      info = await r.json().catch(() => ({}));
      if (!r.ok) { showToast(info.detail || 'Erro ao ler a playlist.', 'error'); return; }
    } catch {
      showToast('Erro de rede ao ler a playlist.', 'error');
      return;
    } finally {
      btn.disabled = false;
    }
    if (!info.is_playlist || info.count <= 1) {
      showToast('Essa URL não parece ser uma playlist — baixando como um único item.', '');
    } else {
      const ok = await showConfirm({
        title: `Baixar ${info.count} vídeo${info.count === 1 ? '' : 's'}?`,
        message: `${info.playlist_title ? `Playlist "${info.playlist_title}" — ` : ''}` +
          `${info.count} vídeo${info.count === 1 ? '' : 's'} encontrado${info.count === 1 ? '' : 's'}` +
          (info.truncated ? ' (limite de 100 por vez — o restante fica de fora).' : '.') +
          ' Cada um vira um item separado na Biblioteca de Mídia.',
        confirmText: 'Baixar todos',
      });
      if (!ok) return;
    }
  } else if (isBatch) {
    const ok = await showConfirm({
      title: `Baixar ${batchUrls.length} link${batchUrls.length === 1 ? '' : 's'}?`,
      message: `${batchUrls.length} URL${batchUrls.length === 1 ? '' : 's'} detectada${batchUrls.length === 1 ? '' : 's'}` +
        (isPlaylist ? ' — cada uma que for playlist/canal expande em vários vídeos.' : '.') +
        ' Cada resultado vira um item separado na Biblioteca de Mídia.',
      confirmText: 'Baixar todos',
    });
    if (!ok) return;
  }

  btn.disabled = true;
  const origLabel = btn.innerHTML;
  btn.innerHTML = 'Enviando…';
  try {
    const fd = new FormData();
    if (isBatch) fd.append('urls', batchUrls.join('\n'));
    else fd.append('url', singleUrl);
    fd.append('media_type', mediaType);
    fd.append('quality', quality);
    fd.append('playlist', isPlaylist ? 'true' : 'false');
    fd.append('subtitles', subtitles ? 'true' : 'false');
    fd.append('sub_langs', subLangs);
    fd.append('auto_subs', autoSubs ? 'true' : 'false');
    fd.append('metadata', metadata ? 'true' : 'false');
    fd.append('thumbnail', thumbnail ? 'true' : 'false');
    fd.append('audio_lang', audioLang);
    // Formato de saída — só o relevante para o tipo escolhido; o outro segue
    // em 'auto' e o backend ignora.
    fd.append('container',
      mediaType === 'video' ? (document.getElementById('adv-container')?.value || 'auto') : 'auto');
    fd.append('audio_format',
      mediaType === 'audio' ? (document.getElementById('adv-audio-format')?.value || 'auto') : 'auto');
    const res = await fetch('/api/download-advanced', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { showToast(data.detail || 'Erro ao iniciar download.', 'error'); return; }
    showToast(
      data.total > 1
        ? `${data.submitted}/${data.total} downloads iniciados${data.truncated ? ' (limite atingido — o restante ficou de fora)' : ''}.`
        : 'Download iniciado.',
      'success');
    if (isBatch) {
      document.getElementById('adv-urls-batch').value = '';
      _updateAdvBatchCount();
    } else {
      document.getElementById('adv-url-input').value = '';
    }
    // Downloads aparecem (com progresso ao vivo) na Biblioteca de Mídia —
    // o polling já existente (_ensureMediaProgressPolling) cuida do resto.
    switchMainTab('media');
  } catch {
    showToast('Erro de rede ao iniciar download.', 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = origLabel;
  }
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

  const _folderRes = resolveModalFolder();
  if (!_folderRes.ok) { showToast(_folderRes.error, 'error'); return; }
  const folder = _folderRes.folder;

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
  clearPendingFiles();
  // The backend creates the destination folder (and ancestors) synchronously
  // during the upload POST, so it already exists — refresh the sidebar tree
  // right away instead of waiting for the transcription to finish.
  if (folder) loadFolders();
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
                       data.phase, data.phase_progress, data.engine_note);

      if (data.status === 'done') {
        _stopPoll(task_id);
        hideProgress();
        await loadHistory();
        await loadStats();
        loadFolders(); // refresh sidebar counts (and any newly-created folder)
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

function _updateRowStatus(filename, status, pct, phase, phasePct, engineNote) {
  // Persist live progress + phase on the in-memory entry so EVERY render path
  // (this poll, the 5s ETA tick, and full re-renders) produces identical output —
  // the % and ETA stay visible the whole time instead of flickering.
  const f = files.find(x => x.file === filename);
  if (f) {
    f.status = status;
    f._progress = pct;
    if (phase) f._phase = phase;
    if (typeof phasePct === 'number') f._phaseProgress = phasePct;
    // Só aparece quando o download caiu num motor alternativo (plano B).
    f._engineNote = engineNote || null;
  }
  const tr = document.querySelector(`#files-tbody tr[data-id="${CSS.escape(filename)}"]`);
  if (tr) {
    const cell = tr.querySelector('.col-status');
    if (cell) cell.innerHTML = renderStatus(status, f || { _progress: pct, _phase: phase, _phaseProgress: phasePct, _engineNote: engineNote });
  }
  // Mirror to the media library tab — download-only tasks live there.
  const mediaTr = document.querySelector(`#media-tbody tr[data-id="${CSS.escape(filename)}"]`);
  if (mediaTr) {
    const mcell = mediaTr.querySelector('.col-status');
    if (mcell) mcell.innerHTML = renderStatus(status, f || { _progress: pct, _phase: phase, _phaseProgress: phasePct, _engineNote: engineNote });
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
    ren:  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/></svg>',
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
          <button type="button" class="btn btn-soft" onclick="dlViewerFile('md')">MD</button>
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
      <button type="button" class="btn btn-soft" onclick="promptRenameFile('${jsAttr(f.id)}')">${ic.ren}Renomear</button>
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

const TOAST_MAX        = 3;      // no máximo 3 simultâneos (regra 224)
const TOAST_MS         = 4500;   // piso de 4s
const TOAST_MS_ERROR   = 9000;   // erro precisa de tempo para ler e agir

function _dismissToast(t) {
  if (!t || t.dataset.leaving) return;
  t.dataset.leaving = '1';
  t.classList.add('leaving');
  // 300ms cobre a animação de saída; o fallback garante a remoção mesmo
  // sob prefers-reduced-motion (onde animationend dispara em ~1ms).
  t.addEventListener('animationend', () => t.remove(), { once: true });
  setTimeout(() => t.remove(), 400);
}

function showToast(msg, type = '') {
  const area = document.getElementById('toast-area');
  if (!area) return;

  // Fila: nunca mais de 3 na tela — o mais antigo sai para o novo entrar.
  const live = Array.from(area.children).filter(el => !el.dataset.leaving);
  for (const old of live.slice(0, Math.max(0, live.length - (TOAST_MAX - 1)))) {
    _dismissToast(old);
  }

  const t = document.createElement('div');
  t.className = 'toast' + (type ? ' ' + type : '');
  // Sem role="alert" aqui: #toast-area já é aria-live="polite". Aninhar
  // role="alert" dentro dela faz o leitor de tela anunciar duas vezes.
  // Icon is trusted static HTML; msg may contain user/backend strings, so use textContent.
  t.innerHTML = TOAST_ICONS[type] || TOAST_ICONS[''];
  const span = document.createElement('span');
  span.textContent = msg;
  t.appendChild(span);

  // Fechar explícito: mensagem que desaparece sozinha não é caminho único.
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'toast-close';
  close.setAttribute('aria-label', 'Fechar notificação');
  close.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" '
    + 'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" '
    + 'aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/>'
    + '<line x1="6" y1="6" x2="18" y2="18"/></svg>';
  close.onclick = () => _dismissToast(t);
  t.appendChild(close);

  area.appendChild(t);

  // Timer pausável: hover e foco congelam a contagem (regra 223) — sem isso
  // o usuário perde a mensagem justo quando tenta lê-la.
  const total = type === 'error' ? TOAST_MS_ERROR : TOAST_MS;
  let remaining = total, startedAt = Date.now(), timer = null;
  const resume = () => {
    if (t.dataset.leaving) return;
    startedAt = Date.now();
    timer = setTimeout(() => _dismissToast(t), remaining);
  };
  const pause = () => {
    if (timer === null) return;
    clearTimeout(timer); timer = null;
    remaining = Math.max(600, remaining - (Date.now() - startedAt));
  };
  t.addEventListener('mouseenter', pause);
  t.addEventListener('mouseleave', resume);
  t.addEventListener('focusin',  pause);
  t.addEventListener('focusout', resume);
  resume();
  return t;
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
//  YT-DLP DESATUALIZADO — o YouTube muda o extrator com frequência; uma versão
//  velha do yt-dlp costuma quebrar TODOS os downloads de uma vez só. Avisa
//  proativamente e oferece atualização em um clique, em vez do usuário só
//  descobrir isso quando downloads começam a falhar silenciosamente.
// ═══════════════════════════════════════════════════════════════
const _YTDLP_DISMISS_KEY = 'wt:ytdlp-dismissed-until';

async function checkYtdlpOutdated() {
  // Pula se o usuário dispensou o aviso recentemente (24h de cooldown)
  try {
    const until = parseFloat(localStorage.getItem(_YTDLP_DISMISS_KEY) || '0');
    if (until && Date.now() < until) return;
  } catch { /* localStorage pode estar bloqueado em alguns contextos — segue mesmo assim */ }

  try {
    const res = await fetch('/api/ytdlp/status');
    if (!res.ok) return;
    const data = await res.json();
    if (!data.outdated) {
      // Limpa um banner que ficou preso de uma checagem anterior — sem isso,
      // uma aba aberta há horas nunca saberia que o servidor foi reiniciado
      // e atualizado nesse meio tempo (só rechecávamos 1x, no carregamento).
      document.getElementById('ytdlp-outdated-banner').style.display = 'none';
      return; // inclui o caso "latest desconhecido" (offline) — nunca alarma à toa
    }

    document.getElementById('ytdlp-installed-ver').textContent = data.installed || '?';
    document.getElementById('ytdlp-latest-ver').textContent = data.latest || '?';
    document.getElementById('ytdlp-outdated-banner').style.display = 'flex';
  } catch { /* falha de rede — só não mostra o banner desta vez */ }
}

function dismissYtdlpBanner() {
  // Dispensa por 24h para não incomodar a cada atualização de página.
  try {
    localStorage.setItem(_YTDLP_DISMISS_KEY, String(Date.now() + 24 * 3600 * 1000));
  } catch {}
  const banner = document.getElementById('ytdlp-outdated-banner');
  if (banner) banner.style.display = 'none';
}

async function updateYtdlp() {
  const btn = document.getElementById('ytdlp-update-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Atualizando…'; }
  try {
    const res = await fetch('/api/ytdlp/update', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(data.detail || 'Erro ao atualizar.', 'error');
      return;
    }
    document.getElementById('ytdlp-outdated-banner').style.display = 'none';
    await showAlert({
      title: 'Downloader atualizado',
      message: 'A nova versão só entra em uso depois de reiniciar o app — feche e abra o Whisper Transcritor novamente antes de baixar do YouTube.',
      confirmText: 'Entendi',
    });
  } catch {
    showToast('Erro de rede ao atualizar.', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Atualizar agora'; }
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
//  CUSTOM SELECT — dropdown estilizado sobre o <select> nativo.
//  O <select> continua sendo a fonte da verdade (value / evento change /
//  opções populadas dinamicamente seguem funcionando); só trocamos o
//  popup nativo do sistema operacional por uma lista que combina com o app.
// ═══════════════════════════════════════════════════════════════
function enhanceSelects(root = document) {
  root.querySelectorAll('select.select:not([data-cs])').forEach(_initCustomSelect);
}

function _closeAllCustomSelects() {
  document.querySelectorAll('.cs.cs-open').forEach(cs => cs._csClose && cs._csClose());
}

function _initCustomSelect(sel) {
  sel.setAttribute('data-cs', '1');

  const wrap = document.createElement('div');
  wrap.className = 'cs';
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);
  sel.classList.add('cs-native');
  sel.tabIndex = -1;

  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'cs-trigger';
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  const lbl = sel.getAttribute('aria-label');
  if (lbl) trigger.setAttribute('aria-label', lbl);
  trigger.innerHTML = '<span class="cs-value"></span>'
    + '<svg class="cs-chev" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>';

  const menu = document.createElement('div');
  menu.className = 'cs-menu';
  menu.setAttribute('role', 'listbox');

  wrap.appendChild(trigger);
  wrap.appendChild(menu);

  let activeIdx = -1;

  function buildMenu() {
    menu.innerHTML = '';
    Array.from(sel.options).forEach((opt, i) => {
      const item = document.createElement('div');
      item.className = 'cs-option';
      item.setAttribute('role', 'option');
      item.innerHTML = '<span class="cs-opt-label"></span>'
        + '<svg class="cs-check" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>';
      item.querySelector('.cs-opt-label').textContent = opt.textContent;
      if (opt.disabled) item.setAttribute('aria-disabled', 'true');
      // mousedown (not click) para não perder o foco do trigger antes de commitar
      item.addEventListener('mousedown', e => {
        e.preventDefault();
        if (!opt.disabled) commit(i);
      });
      menu.appendChild(item);
    });
    syncValue();
  }

  function syncValue() {
    const o = sel.options[sel.selectedIndex];
    trigger.querySelector('.cs-value').textContent = o ? o.textContent : '';
    Array.from(menu.children).forEach((it, i) => {
      const on = i === sel.selectedIndex;
      it.classList.toggle('selected', on);
      it.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }

  function setActive(i) {
    const items = menu.children;
    if (activeIdx >= 0 && items[activeIdx]) items[activeIdx].classList.remove('active');
    activeIdx = i;
    if (items[i]) { items[i].classList.add('active'); items[i].scrollIntoView({ block: 'nearest' }); }
  }

  function commit(i) {
    if (i < 0 || i >= sel.options.length || sel.options[i].disabled) return;
    if (sel.selectedIndex !== i) {
      sel.selectedIndex = i;
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    }
    syncValue();
    close();
    trigger.focus();
  }

  function position() {
    const r = trigger.getBoundingClientRect();
    menu.style.width = r.width + 'px';
    menu.style.left = r.left + 'px';
    const h = Math.min(menu.scrollHeight, 280);
    const below = window.innerHeight - r.bottom;
    // Abre para cima se não couber embaixo e houver mais espaço em cima
    if (below < h + 12 && r.top > below) {
      menu.style.top = Math.max(8, r.top - 6 - h) + 'px';
    } else {
      menu.style.top = (r.bottom + 6) + 'px';
    }
  }

  function open() {
    _closeAllCustomSelects();
    wrap.classList.add('cs-open');
    menu.classList.add('open');
    trigger.setAttribute('aria-expanded', 'true');
    position();
    setActive(sel.selectedIndex);
  }
  function close() {
    wrap.classList.remove('cs-open');
    menu.classList.remove('open');
    trigger.setAttribute('aria-expanded', 'false');
    if (activeIdx >= 0 && menu.children[activeIdx]) menu.children[activeIdx].classList.remove('active');
    activeIdx = -1;
  }
  wrap._csClose = close;

  trigger.addEventListener('click', e => {
    e.stopPropagation();
    menu.classList.contains('open') ? close() : open();
  });

  trigger.addEventListener('keydown', e => {
    const isOpen = menu.classList.contains('open');
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!isOpen) { open(); return; }
      let i = activeIdx;
      const step = e.key === 'ArrowDown' ? 1 : -1;
      do { i += step; } while (i >= 0 && i < sel.options.length && sel.options[i].disabled);
      if (i >= 0 && i < sel.options.length) setActive(i);
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      isOpen ? commit(activeIdx) : open();
    } else if (e.key === 'Escape') {
      if (isOpen) { e.preventDefault(); close(); }
    } else if (e.key === 'Tab') {
      close();
    }
  });

  // Mudanças programáticas de valor / opções mantêm o custom em sincronia
  sel.addEventListener('change', syncValue);
  new MutationObserver(buildMenu).observe(sel, { childList: true });

  buildMenu();
}

// Fecha menus ao clicar fora, rolar o container (que não seja o próprio menu) ou redimensionar
document.addEventListener('click', () => _closeAllCustomSelects());
window.addEventListener('resize', () => _closeAllCustomSelects());
document.addEventListener('scroll', e => {
  if (e.target && e.target.closest && e.target.closest('.cs-menu')) return;
  _closeAllCustomSelects();
}, true);

// ═══════════════════════════════════════════════════════════════
//  BOOT
// ═══════════════════════════════════════════════════════════════
init();
enhanceSelects();
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
        _mediaFiles = await res.json();
        applyMediaFilterAndRender();
        _ensureMediaProgressPolling(_mediaFiles);
      } catch { /* ignore */ }
    }

    // Regra de visibilidade: a Biblioteca de Mídia só lista o que ainda está
    // fisicamente salvo na máquina (on_disk) — exceto downloads em andamento
    // (queued/processing), que ficam visíveis para permitir cancelar mesmo
    // antes do arquivo existir por completo no disco.
    function _isMediaOnMachine(m) {
      return m.on_disk || m.status === 'queued' || m.status === 'processing';
    }

    function _isMediaFailed(m) {
      return m.status === 'error' || m.status === 'cancelled';
    }

    function _getVisibleMedia() {
      // "Falharam" é uma visão à parte: itens com erro/cancelado normalmente
      // não estão on_disk, então ficariam escondidos pelo filtro padrão — sem
      // esse chip não haveria como selecioná-los pra tentar de novo.
      if (_mediaView.type === 'failed') return _mediaFiles.filter(_isMediaFailed);
      let arr = _mediaFiles.filter(_isMediaOnMachine);
      if (_mediaView.type !== 'all') arr = arr.filter(m => m.type === _mediaView.type);
      return arr;
    }

    function setMediaTypeFilter(type) {
      _mediaView.type = type;
      document.querySelectorAll('.chip[data-mtype]').forEach(el => {
        el.setAttribute('aria-pressed', el.dataset.mtype === type);
      });
      const sel = document.getElementById('mtype-filter-select');
      if (sel) sel.value = type;
      applyMediaFilterAndRender();
    }

    function _renderMediaTypeCounts() {
      const onMachine = _mediaFiles.filter(_isMediaOnMachine);
      const counts = { all: onMachine.length, audio: 0, video: 0,
                        failed: _mediaFiles.filter(_isMediaFailed).length };
      for (const m of onMachine) if (m.type === 'audio' || m.type === 'video') counts[m.type]++;
      const mtypeLabels = { all: 'Todos', audio: 'Áudio', video: 'Vídeo', failed: 'Falharam' };
      for (const k of Object.keys(counts)) {
        const el = document.getElementById('mchip-count-' + k);
        if (el) el.textContent = counts[k];
        const opt = document.getElementById('opt-mtype-' + k);
        if (opt) opt.textContent = `${mtypeLabels[k]} (${counts[k]})`;
      }
    }

    // Soma o espaço ocupado pelo conjunto atualmente visível (respeita o filtro
    // de tipo) — só conta arquivos que estão de fato on_disk (downloads ainda em
    // andamento não têm tamanho final ainda).
    function _renderMediaSpaceSummary(visible) {
      const el = document.getElementById('media-space-summary');
      if (!el) return;
      const onDisk = visible.filter(m => m.on_disk);
      const totalBytes = onDisk.reduce((s, m) => s + (m.size_bytes || 0), 0);
      el.textContent = onDisk.length
        ? `${onDisk.length} arquivo${onDisk.length !== 1 ? 's' : ''} · ${formatBytes(totalBytes)} ocupados`
        : '—';
    }

    function applyMediaFilterAndRender() {
      _renderMediaTypeCounts();
      const visible = _getVisibleMedia();
      _renderMediaSpaceSummary(visible);
      renderMedia(visible);
      syncMediaBulkBar();
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
        const isFailed = (f.status === 'error' || f.status === 'cancelled');
        // Show cancel option for in-flight downloads; failed/cancelled items get
        // a retry option; the regular file-management items only make sense
        // when there's actually a file on disk to download.
        const actionItems = isActive ? `
                <div class="dd-item danger" role="menuitem" tabindex="-1" onclick="cancelMediaDownload('${jsAttr(f.file)}')">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
                  Cancelar download
                </div>` : `
                ${isFailed ? `
                <div class="dd-item" role="menuitem" tabindex="-1" onclick="retryMediaFile('${jsAttr(f.file)}')">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
                  Tentar novamente
                </div>
                <div class="dd-sep"></div>` : ''}
                ${f.on_disk ? `
                <div class="dd-item" role="menuitem" tabindex="-1" onclick="dlMediaFile('${jsAttr(f.file)}')">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                  Baixar Original para o PC
                </div>
                <div class="dd-sep"></div>` : ''}
                ${_me.is_admin ? (f.visibility === 'public' ? `
                <div class="dd-item" role="menuitem" tabindex="-1" onclick="setMediaVisibility('${jsAttr(f.file)}', false)">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                  Tornar privado
                </div>` : `
                <div class="dd-item" role="menuitem" tabindex="-1" onclick="setMediaVisibility('${jsAttr(f.file)}', true)">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
                  Publicar na Área Pública
                </div>`) + '<div class="dd-sep"></div>' : ''}
                <div class="dd-item danger" role="menuitem" tabindex="-1" onclick="deleteMediaFile('${jsAttr(f.file)}')">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>
                  Excluir Mídia Hospedada
                </div>`;
        tr.innerHTML = `
          <td class="col-check"><input type="checkbox" class="checkbox" aria-label="Selecionar ${esc(f.name)}"
              ${_mediaSelected.has(f.id) ? 'checked' : ''} onclick="handleMediaCheckboxClick('${jsAttr(f.id)}', this, event)" /></td>
          <td>
            <div class="file-name-row">
              ${f.on_disk ? _fileTypeIconHtml(f.file) : ''}
              <div class="file-name">${esc(f.name)}</div>
            </div>
            ${(f.visibility === 'public' && _me.is_admin) ? `
            <div class="file-tags"><span class="ftag ftag-public" title="Visível para os funcionários na Área Pública"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>Pública</span></div>` : ''}
          </td>
          <td class="col-date" data-label="Enviado"><div class="file-date">${esc(f.date)}</div></td>
          <td class="col-dur" data-label="Duração"><div class="file-dur">${formatBytes(f.size_bytes)}</div></td>
          <td class="col-status" data-label="Status">${renderStatus(f.status, null)}</td>
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

    // ═══════════════════════════════════════════════════════════════
    //  MEDIA — SELEÇÃO EM LOTE
    // ═══════════════════════════════════════════════════════════════
    // Mesmo padrão do shift+click da tabela de Transcrições (handleCheckboxClick):
    // clicar com Shift estende a seleção do último item clicado até este,
    // aplicando o estado (marcar/desmarcar) do clique atual a todo o intervalo.
    let _lastCheckedMediaId = null;

    function handleMediaCheckboxClick(id, cb, ev) {
      ev.stopPropagation();
      const checked = cb.checked; // o clique nativo já alternou o estado
      if (ev.shiftKey && _lastCheckedMediaId && _lastCheckedMediaId !== id) {
        const visible = _getVisibleMedia();
        const a = visible.findIndex(m => m.id === _lastCheckedMediaId);
        const b = visible.findIndex(m => m.id === id);
        if (a !== -1 && b !== -1) {
          const [lo, hi] = a < b ? [a, b] : [b, a];
          for (let i = lo; i <= hi; i++) {
            const mid = visible[i].id;
            checked ? _mediaSelected.add(mid) : _mediaSelected.delete(mid);
          }
          // Shift+click em alguns browsers também seleciona texto da página — limpa.
          try { window.getSelection().removeAllRanges(); } catch {}
          renderMedia(visible);
          syncMediaBulkBar();
          _lastCheckedMediaId = id;
          return;
        }
      }
      checked ? _mediaSelected.add(id) : _mediaSelected.delete(id);
      _lastCheckedMediaId = id;
      syncMediaBulkBar();
    }

    function toggleMediaAll(cb) {
      const visible = _getVisibleMedia();
      visible.forEach(m => cb.checked ? _mediaSelected.add(m.id) : _mediaSelected.delete(m.id));
      renderMedia(visible);
      syncMediaBulkBar();
    }

    function _selectedVisibleMediaIds() {
      const visible = _getVisibleMedia();
      return visible.filter(m => _mediaSelected.has(m.id)).map(m => m.id);
    }

    function syncMediaBulkBar() {
      const bar = document.getElementById('media-bulk-bar');
      if (!bar) return;
      const visible = _getVisibleMedia();
      const visibleIds = _selectedVisibleMediaIds();
      const count = visibleIds.length;
      document.getElementById('media-bulk-count').textContent =
        `${count} selecionado${count !== 1 ? 's' : ''}`;
      bar.classList.toggle('show', count > 0);
      const headCb = document.getElementById('media-check-all');
      if (headCb) {
        headCb.checked = count === visible.length && visible.length > 0;
        headCb.indeterminate = count > 0 && count < visible.length;
      }
    }

    async function retrySelectedMedia() {
      const ids = _selectedVisibleMediaIds();
      if (!ids.length) return;
      const retryable = ids
        .map(id => _mediaFiles.find(m => m.id === id))
        .filter(m => m && (m.status === 'error' || m.status === 'cancelled'));
      if (!retryable.length) {
        showToast('Nenhum selecionado está com erro ou cancelado.', 'error');
        return;
      }
      const skipped = ids.length - retryable.length;
      const ok = await showConfirm({
        title: `Tentar novamente ${retryable.length} ${retryable.length === 1 ? 'item' : 'itens'}?`,
        message: 'Refaz o download (ou a transcrição, se era esse o pedido original) com a mesma URL.' +
          (skipped ? ` ${skipped} selecionado${skipped === 1 ? '' : 's'} não está${skipped === 1 ? '' : 'ão'} ` +
                     `com erro e será${skipped === 1 ? '' : 'ão'} ignorado${skipped === 1 ? '' : 's'}.` : ''),
        confirmText: 'Tentar novamente',
      });
      if (!ok) return;
      try {
        const fd = new FormData();
        fd.append('files', JSON.stringify(retryable.map(m => m.file)));
        const res = await fetch('/api/retry-batch', { method: 'POST', body: fd });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { showToast(data.detail || 'Erro ao tentar novamente.', 'error'); return; }
        showToast(
          `${data.submitted}/${data.total} reenviado(s)` +
          (data.failed ? `, ${data.failed} falhou(aram).` : '.'),
          'success');
        retryable.forEach(m => _mediaSelected.delete(m.id));
        await loadMedia();
      } catch {
        showToast('Erro de rede ao tentar novamente.', 'error');
      }
    }

    async function deleteSelectedMedia() {
      const ids = _selectedVisibleMediaIds();
      if (!ids.length) return;
      const count = ids.length;
      const ok = await showConfirm({
        title: `Excluir ${count} arquivo${count === 1 ? '' : 's'}`,
        message: 'Os arquivos físicos serão removidos do cofre local e não poderão mais ser re-baixados ou re-transcritos. As transcrições já feitas continuam salvas.',
        confirmText: 'Excluir',
        danger: true,
      });
      if (!ok) return;
      try {
        const fd = new FormData();
        fd.append('files', ids.join(','));
        const res = await fetch('/api/media/cleanup', { method: 'POST', body: fd });
        if (!res.ok) {
          let msg = 'Erro ao excluir.';
          try { const e = await res.json(); if (e.detail) msg = e.detail; } catch {}
          showToast(msg, 'error');
          return;
        }
        const data = await res.json();
        ids.forEach(id => _mediaSelected.delete(id));
        await loadMedia();
        showToast(
          `${data.deleted} arquivo${data.deleted !== 1 ? 's' : ''} excluído(s)` +
          (data.failed?.length ? `, ${data.failed.length} falhou(aram)` : '') + '.',
          'success');
      } catch {
        showToast('Erro ao excluir.', 'error');
      }
    }

    // ── Publicar/despublicar mídia (Biblioteca de Mídia) ──
    // Um vídeo baixado que nunca foi transcrito só existe em media.json, então
    // precisa de um caminho próprio para entrar na Área Pública. O endpoint é o
    // mesmo e atualiza os dois catálogos.
    async function setMediaVisibility(filename, makePublic) {
      closeAllDDs();
      const f = _mediaFiles.find(m => m.file === filename);
      if (makePublic) {
        const ok = await showConfirm({
          title: 'Publicar para os funcionários',
          message: `"${f?.name || filename}" passa a aparecer na Área Pública — quem tem a `
                 + 'senha de acesso vai poder assistir e baixar este arquivo.',
          confirmText: 'Publicar',
        });
        if (!ok) return;
      }
      try {
        await _applyVisibility([filename], makePublic);
        await loadMedia();
        showToast(makePublic ? 'Publicado na Área Pública.' : 'Mídia voltou a ser privada.', 'success');
      } catch (e) {
        showToast(e.message, 'error');
      }
    }

    async function publishSelectedMedia(makePublic) {
      const ids = _selectedVisibleMediaIds();
      if (!ids.length) { showToast('Selecione ao menos um arquivo.', 'error'); return; }
      const ok = await showConfirm({
        title: makePublic ? 'Publicar para os funcionários' : 'Tornar privado',
        message: makePublic
          ? `${ids.length} arquivo(s) passam a ficar disponíveis para quem tem a senha de acesso.`
          : `${ids.length} arquivo(s) saem da Área Pública e voltam a ser visíveis só para você.`,
        confirmText: makePublic ? 'Publicar' : 'Tornar privado',
      });
      if (!ok) return;
      try {
        const res = await _applyVisibility(ids, makePublic);
        _mediaSelected.clear();
        await loadMedia();
        showToast(`${res.changed} ${res.changed === 1 ? 'item atualizado' : 'itens atualizados'}.`, 'success');
      } catch (e) {
        showToast(e.message, 'error');
      }
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

    async function retryMediaFile(filename) {
      closeAllDDs();
      try {
        const res = await fetch(`/api/retry/${encodeURIComponent(filename)}`, { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { showToast(data.detail || 'Erro ao tentar novamente.', 'error'); return; }
        showToast('Reenviado — acompanhe o progresso na lista.', 'success');
        await loadMedia();
      } catch {
        showToast('Erro de rede ao tentar novamente.', 'error');
      }
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

// ═══════════════════════════════════════════════════════════════
// Redes Sociais — coleta (ego-lite), mosaico 9:16, download + transcrição
// ═══════════════════════════════════════════════════════════════
let _socialRows = [];          // linhas do dataset carregado
let _socialProfile = {};       // perfil da coleta (avatar, seguidores…)
let _socialSel = new Set();    // códigos selecionados (clique / Cmd / Shift)
let _socialLastIdx = null;     // último índice clicado (para range com Shift)
let _socialMode = 'profile';
let _socialNetwork = 'instagram';

const _SOCIAL_NET_UI = {
  instagram: { label: 'Perfil do Instagram', ph: '@perfil', period: true },
  tiktok:    { label: 'Perfil do TikTok',    ph: '@perfil', period: false },
  youtube:   { label: 'Canal do YouTube',    ph: '@canal ou URL do canal', period: false },
  facebook:  { label: 'Página do Facebook',  ph: 'nome da página ou URL', period: false },
};
function setSocialNetwork(net) {
  _socialNetwork = net;
  const ui = _SOCIAL_NET_UI[net] || _SOCIAL_NET_UI.instagram;
  const lbl = document.getElementById('social-username-label');
  const inp = document.getElementById('social-username');
  const period = document.getElementById('social-period-field');
  if (lbl) lbl.textContent = ui.label;
  if (inp) inp.placeholder = ui.ph;
  if (period) period.style.display = ui.period ? '' : 'none';   // período só no Instagram
}
let _socialDatasetId = null;
let _socialInited = false;

function socialErCls(er) {
  if (er == null) return 'lo';
  if (er >= 5) return 'hi';
  if (er >= 2) return 'mid';
  return 'lo';
}
function socialAvg(rows, f) {
  const v = rows.map(f).filter(x => x != null);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}

function renderSocialHeader() {
  const p = _socialProfile || {};
  const u = p.username || _socialDatasetId || '?';
  const initial = esc(String(u)[0].toUpperCase());
  const pic = p.profile_pic
    ? `<img src="/api/social/thumb?url=${encodeURIComponent(p.profile_pic)}" onerror="this.remove()" alt="">` : '';
  const stats = [
    p.followers ? `${socialFmtNum(p.followers)} seguidores` : '',
    p.posts_total ? `${socialFmtNum(p.posts_total)} posts no perfil` : '',
    `${_socialRows.length} posts coletados`,
  ].filter(Boolean).join('&nbsp;·&nbsp;');
  document.getElementById('social-profile-head').innerHTML = `
    <span class="social-avatar">${initial}${pic}</span>
    <div class="social-prof-meta">
      <div class="social-prof-name">@${esc(u)}${p.full_name ? ` <span>· ${esc(p.full_name)}</span>` : ''}</div>
      <div class="social-prof-stats">${stats}</div>
    </div>
    <div class="social-prof-actions">
      <button type="button" class="btn btn-soft" id="social-export-btn" onclick="socialExport()">${sic('dl')} Exportar Excel</button>
    </div>`;
}

function renderSocialKPIs(rows) {
  const box = document.getElementById('social-kpis');
  if (!box) return;
  const viewsAvg = socialAvg(rows, r => r.views);
  const erAvg = socialAvg(rows, r => r.er);
  const metric = (icon, label, val) =>
    `<div class="social-metric"><div class="social-metric-l">${sic(icon)} ${label}</div><div class="social-metric-v">${val}</div></div>`;
  box.innerHTML =
    metric('eye', 'Views médias', viewsAvg != null ? socialFmtNum(Math.round(viewsAvg)) : '—') +
    metric('heart', 'Likes médios', socialFmtNum(Math.round(socialAvg(rows, r => r.likes) || 0))) +
    metric('comment', 'Comentários médios', socialFmtNum(Math.round(socialAvg(rows, r => r.comments) || 0))) +
    metric('zap', 'ER médio', (erAvg != null ? erAvg.toFixed(2) : '—') + '<small> %</small>');
}

function socialThumbUrl(u) { return '/api/social/thumb?url=' + encodeURIComponent(u); }
function socialMediaUrl(u) { return '/api/social/media?url=' + encodeURIComponent(u); }

// Ícones SVG inline (mesma linguagem visual do app — nada de emoji).
const _SIC = {
  play: '<polygon points="6 4 19 12 6 20 6 4"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
  heart: '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/>',
  comment: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  share: '<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>',
  dl: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="3" y2="15"/>',
  ext: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  film: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M7 3v18M17 3v18M3 7.5h4M3 12h18M3 16.5h4M17 7.5h4M17 16.5h4"/>',
  image: '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
  layers: '<path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
  zap: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
  hash: '<line x1="4" x2="20" y1="9" y2="9"/><line x1="4" x2="20" y1="15" y2="15"/><line x1="10" x2="8" y1="3" y2="21"/><line x1="16" x2="14" y1="3" y2="21"/>',
  filetext: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" x2="8" y1="13" y2="13"/><line x1="16" x2="8" y1="17" y2="17"/>',
  chart: '<line x1="12" x2="12" y1="20" y2="10"/><line x1="18" x2="18" y1="20" y2="4"/><line x1="6" x2="6" y1="20" y2="16"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
};
function sic(name) { return `<svg class="sic" viewBox="0 0 24 24" aria-hidden="true">${_SIC[name] || ''}</svg>`; }
const _SOCIAL_TYPE_ICON = { 'Reel/Vídeo': 'film', 'Foto': 'image', 'Carrossel': 'layers' };

function socialFmtNum(n) {
  if (n === null || n === undefined) return '—';
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace('.0', '') + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1).replace('.0', '') + 'K';
  return String(n);
}
function socialFmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: '2-digit' });
}
function socialFmtDur(s) {
  if (!s) return '';
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return m ? `${m}:${String(sec).padStart(2, '0')}` : `0:${String(sec).padStart(2, '0')}`;
}

async function initSocialTab() {
  if (_socialInited) return;
  _socialInited = true;
  // Estado inicial: mostra o empty-state explicativo enquanto não há coleta aberta.
  if (document.getElementById('social-analytics').style.display === 'none') {
    document.getElementById('social-empty').style.display = 'flex';
  }
  try {
    const st = await (await fetch('/api/social/status')).json();
    const el = document.getElementById('social-engine-status');
    if (st.ego_browser) {
      el.textContent = 'ego lite conectado';
      el.className = 'social-engine-status ok';
    } else {
      el.textContent = 'ego lite não encontrado — abra e faça login no Instagram';
      el.className = 'social-engine-status warn';
    }
  } catch (e) { /* ignore */ }
  await refreshSocialDatasets();
}

async function refreshSocialDatasets() {
  try {
    const list = await (await fetch('/api/social/datasets')).json();
    const picker = document.getElementById('social-dataset-picker');
    if (!list.length) { picker.style.display = 'none'; return; }
    picker.style.display = '';
    picker.innerHTML = '<option value="">Coletas anteriores…</option>' +
      list.map(d => `<option value="${esc(d.id)}">@${esc(d.username)} · ${d.count} posts · ${socialFmtDate(d.collected_at)}</option>`).join('');
  } catch (e) { /* ignore */ }
}

function setSocialMode(mode) {
  _socialMode = mode;
  document.getElementById('social-mode-profile').setAttribute('aria-selected', mode === 'profile');
  document.getElementById('social-mode-urls').setAttribute('aria-selected', mode === 'urls');
  document.getElementById('social-mode-profile').tabIndex = mode === 'profile' ? 0 : -1;
  document.getElementById('social-mode-urls').tabIndex = mode === 'urls' ? 0 : -1;
  document.getElementById('social-input-profile').style.display = mode === 'profile' ? '' : 'none';
  document.getElementById('social-input-urls').style.display = mode === 'urls' ? '' : 'none';
}

async function startSocialCollect() {
  const btn = document.getElementById('social-collect-btn');
  const hint = document.getElementById('social-collect-hint');
  let url, fd = new FormData();
  if (_socialMode === 'profile') {
    const username = document.getElementById('social-username').value.trim();
    if (!username) { showToast('Informe o perfil.', 'error'); return; }
    fd.append('username', username);
    fd.append('max_posts', document.getElementById('social-max').value || '60');
    fd.append('since_days', document.getElementById('social-period').value || '');
    fd.append('platform', (document.getElementById('social-network') || {}).value || 'instagram');
    url = '/api/social/collect';
  } else {
    const urls = document.getElementById('social-urls').value.trim();
    if (!urls) { showToast('Cole ao menos uma URL.', 'error'); return; }
    fd.append('urls', urls);
    url = '/api/social/collect-urls';
  }
  btn.disabled = true;
  hint.textContent = '';
  document.getElementById('social-empty').style.display = 'none';   // some o empty-state enquanto coleta
  document.getElementById('social-progress').style.display = '';
  document.getElementById('social-progress').textContent = 'Conectando ao ego lite…';
  try {
    const r = await fetch(url, { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || 'falha ao coletar');
    pollSocialJob(j.job_id);
  } catch (e) {
    btn.disabled = false;
    hint.textContent = '';
    document.getElementById('social-progress').style.display = 'none';
    showToast('Erro ao coletar: ' + e.message, 'error');
  }
}

function pollSocialJob(jobId, onDone) {
  // Timer LOCAL por chamada: ações sociais concorrentes (coletar, baixar um
  // card, baixar+transcrever) não podem cancelar o polling uma da outra.
  const prog = document.getElementById('social-progress');
  const isCollect = !onDone;   // coleta usa o fluxo padrão (mostra progresso, recarrega)
  const timer = setInterval(async () => {
    try {
      const j = await (await fetch('/api/social/job/' + jobId)).json();
      const p = j.progress;
      if (p && isCollect) {
        prog.textContent = ('collected' in p)
          ? `Coletando posts… ${p.collected}${p.target ? ' de ' + p.target : ''}`
          : `Processando… ${p.done} de ${p.target}`;
      }
      if (j.status === 'done') {
        clearInterval(timer);
        if (onDone) { onDone(j.result); }
        else {
          document.getElementById('social-collect-btn').disabled = false;
          document.getElementById('social-collect-hint').textContent = '';
          prog.style.display = 'none';
          await refreshSocialDatasets();
          if (j.result && j.result.ds_id) loadSocialDataset(j.result.ds_id);
        }
      } else if (j.status === 'error') {
        clearInterval(timer);
        showToast((isCollect ? 'Coleta falhou: ' : 'Ação falhou: ') + (j.error || ''), 'error');
        prog.style.display = 'none';   // erro vai pro toast; barra some (evita spinner + texto de erro)
        if (isCollect) {
          document.getElementById('social-collect-btn').disabled = false;
          document.getElementById('social-collect-hint').textContent = '';
        } else {
          onDone(null);   // deixa o chamador reabilitar botões mesmo em erro do job
        }
      }
    } catch (e) { /* keep polling */ }
  }, 1200);
}

async function loadSocialDataset(dsId) {
  if (!dsId) return;
  _socialDatasetId = dsId;
  _socialSel.clear();
  const prog = document.getElementById('social-progress');
  prog.style.display = '';
  prog.textContent = 'Carregando coleta…';
  try {
    const ds = await (await fetch('/api/social/dataset/' + encodeURIComponent(dsId))).json();
    _socialRows = ds.rows || [];
    _socialProfile = ds.profile || {};
    renderSocialHeader();
    document.getElementById('social-analytics').style.display = '';
    document.getElementById('social-empty').style.display = 'none';
    prog.style.display = 'none';
    renderSocialGrid();
  } catch (e) {
    prog.textContent = 'Erro ao carregar: ' + e.message;
  }
}

function socialFilteredRows() {
  const fmt = document.getElementById('social-format').value;
  const q = document.getElementById('social-search').value.trim().toLowerCase();
  const sort = document.getElementById('social-sort').value;
  let rows = _socialRows.slice();
  if (fmt === 'video') rows = rows.filter(r => r.is_video);
  else if (fmt !== 'all') rows = rows.filter(r => r.type === fmt);
  if (q) rows = rows.filter(r => (r.caption || '').toLowerCase().includes(q));
  const key = { date: 'ts', er: 'er', views: 'views', likes: 'likes', comments: 'comments', reshares: 'reshares' }[sort];
  rows.sort((a, b) => (b[key] || 0) - (a[key] || 0));
  return rows;
}

function renderSocialGrid() {
  const grid = document.getElementById('social-grid');
  const empty = document.getElementById('social-empty');
  if (!_socialRows.length) {
    grid.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  const rows = socialFilteredRows();
  grid.innerHTML = rows.length ? rows.map(r => {
    const isSel = _socialSel.has(r.code);
    const plat = r.platform || 'Instagram';
    const thumb = r.thumb_url ? socialThumbUrl(r.thumb_url) : '';
    const vid = (r.media_urls || []).find(m => m.type === 'video');
    const canPreview = vid && plat === 'Instagram';   // só o IG entrega URL de CDN direta p/ o proxy
    const typeLabel = r.type === 'Reel/Vídeo' ? 'Reel' : r.type;
    const platBadge = plat !== 'Instagram' ? `<span class="social-badge social-plat">${esc(plat)}</span>` : '';
    const typeBadge = `<span class="social-badge">${sic(_SOCIAL_TYPE_ICON[r.type] || 'image')} ${esc(typeLabel)}</span>`;
    const durBadge = r.duration_s ? `<span class="social-dur">${sic('play')} ${socialFmtDur(r.duration_s)}</span>` : '';
    const erBadge = (r.er !== null && r.er !== undefined)
      ? `<span class="social-er social-er-${socialErCls(r.er)}">ER ${r.er}%</span>` : '';
    const cd = esc(r.code);
    return `
    <div class="social-card${isSel ? ' selected' : ''}" data-code="${cd}" role="button" aria-pressed="${isSel}"
         tabindex="0" onclick="socialCardClick('${cd}', event)"
         onkeydown="if(event.key===' '||event.key==='Enter'){event.preventDefault();socialCardClick('${cd}',event)}">
      <span class="social-check" aria-hidden="true">${sic('check')}</span>
      <div class="social-thumb" ${canPreview ? `data-video="${esc(socialMediaUrl(vid.url))}"` : ''} onmouseenter="socialHover(this,true)" onmouseleave="socialHover(this,false)">
        ${thumb ? `<img loading="lazy" src="${thumb}" alt="" onerror="this.style.display='none'">` : '<div class="social-noimg">sem capa</div>'}
        <div class="social-badges">${platBadge}${typeBadge}</div>
        ${erBadge}
        ${durBadge}
        <div class="social-thumb-actions">
          <button type="button" class="social-iconbtn" title="Baixar mídia" aria-label="Baixar mídia" onclick="event.stopPropagation();socialQuickDownload('${cd}', this)">${sic('dl')}</button>
          <button type="button" class="social-iconbtn" title="Baixar métricas (CSV)" aria-label="Baixar métricas" onclick="event.stopPropagation();socialDownloadMetrics('${cd}')">${sic('chart')}</button>
          <button type="button" class="social-iconbtn" title="Baixar comentários (CSV)" aria-label="Baixar comentários" onclick="event.stopPropagation();socialFetchComments('${cd}', this)">${sic('comment')}</button>
          <a class="social-iconbtn" href="${esc(r.url)}" target="_blank" rel="noopener" title="Abrir na rede" aria-label="Abrir na rede" onclick="event.stopPropagation()">${sic('ext')}</a>
        </div>
      </div>
      <div class="social-meta">
        <div class="social-stats">
          ${r.views != null ? `<span title="Views">${sic('eye')} ${socialFmtNum(r.views)}</span>` : ''}
          <span title="Likes">${sic('heart')} ${socialFmtNum(r.likes)}</span>
          <span title="Comentários">${sic('comment')} ${socialFmtNum(r.comments)}</span>
          ${r.reshares ? `<span title="Reposts">${sic('share')} ${socialFmtNum(r.reshares)}</span>` : ''}
          <span class="social-date">${socialFmtDate(r.date)}</span>
        </div>
        <div class="social-caption" title="${esc(r.caption || '')}">${esc((r.caption || '').slice(0, 90)) || '<em>sem legenda</em>'}</div>
      </div>
    </div>`;
  }).join('') : '<div class="social-insight-empty" style="padding:32px 0;text-align:center">Nenhum post nos filtros atuais.</div>';
  renderSocialKPIs(rows);
  const rr = document.getElementById('social-resrow');
  if (rr) rr.innerHTML = `<b>${rows.length}</b> de ${_socialRows.length} posts nos filtros ativos` +
    ` · clique para selecionar · <b>Shift</b>/<b>⌘</b>+clique p/ vários · passe o mouse p/ prever`;
  updateSocialActionbar();
  if (_socialInsightsOpen) renderSocialInsights();
}

// ── Seleção: clique alterna; Shift+clique = intervalo; Cmd/Ctrl+clique = alterna ──
function socialCardClick(code, event) {
  const rows = socialFilteredRows();
  const idx = rows.findIndex(r => r.code === code);
  if (event && event.shiftKey && _socialLastIdx !== null && _socialLastIdx < rows.length) {
    const a = Math.min(_socialLastIdx, idx), b = Math.max(_socialLastIdx, idx);
    for (let i = a; i <= b; i++) _socialSel.add(rows[i].code);
  } else {
    if (_socialSel.has(code)) _socialSel.delete(code);
    else _socialSel.add(code);
  }
  _socialLastIdx = idx;
  renderSocialGrid();
}

function socialToggleAll(cb) {
  const rows = socialFilteredRows();
  if (cb.checked) rows.forEach(r => _socialSel.add(r.code));
  else rows.forEach(r => _socialSel.delete(r.code));
  renderSocialGrid();
}

function socialClearSel() {
  _socialSel.clear();
  _socialLastIdx = null;
  renderSocialGrid();
}

function updateSocialActionbar() {
  const n = _socialSel.size;
  document.getElementById('social-actionbar').style.display = n ? '' : 'none';
  // A barra é position:fixed — sem reservar espaço no fim do mosaico ela cobre
  // a última fileira de cards (e a linha com foco de teclado). Regras 340/573.
  document.getElementById('social-analytics')?.classList.toggle('com-actionbar', !!n);
  const byCode = new Map(_socialRows.map(r => [r.code, r]));
  let vids = 0;
  _socialSel.forEach(c => { const r = byCode.get(c); if (r && r.is_video) vids++; });
  document.getElementById('social-sel-count').textContent = n + (n === 1 ? ' selecionado' : ' selecionados');
  const bt = document.getElementById('social-btn-transcribe');
  bt.disabled = vids === 0;
  bt.textContent = vids ? `Transcrever (${vids})` : 'Transcrever';
  const all = document.getElementById('social-select-all');
  if (all) { const rows = socialFilteredRows(); all.checked = rows.length > 0 && rows.every(r => _socialSel.has(r.code)); }
}

function socialHover(thumbEl, on) {
  const src = thumbEl.getAttribute('data-video');
  if (!src) return;
  if (on) {
    if (thumbEl.querySelector('video')) return;
    const v = document.createElement('video');
    v.src = src; v.muted = true; v.loop = true; v.playsInline = true; v.className = 'social-preview';
    thumbEl.appendChild(v);
    v.play().catch(() => {});
  } else {
    const v = thumbEl.querySelector('video');
    if (v) { v.pause(); v.remove(); }
  }
}

// ── Download de MÉTRICAS (CSV, client-side) — individual ou em lote ──
function _socialCsv(rows) {
  const cols = [['code', 'codigo'], ['url', 'url'], ['type', 'tipo'], ['date', 'data'],
    ['views', 'views'], ['likes', 'likes'], ['comments', 'comentarios'], ['reshares', 'reposts'],
    ['er', 'er_pct'], ['duration_s', 'duracao_s'], ['hashtags', 'hashtags'], ['caption', 'legenda']];
  const cell = v => {
    let s = v == null ? '' : Array.isArray(v) ? v.join(' ') : String(v);
    s = s.replace(/"/g, '""').replace(/\r?\n/g, ' ');
    return /[";\n]/.test(s) ? `"${s}"` : s;
  };
  const head = cols.map(c => c[1]).join(';');
  const body = rows.map(r => cols.map(c => cell(r[c[0]])).join(';'));
  return '﻿' + [head, ...body].join('\r\n');
}
function socialDownloadMetrics(code) {
  const codes = code ? [code] : [..._socialSel];
  const byCode = new Map(_socialRows.map(r => [r.code, r]));
  const rows = codes.map(c => byCode.get(c)).filter(Boolean);
  if (!rows.length) { showToast('Selecione ao menos um item.', 'error'); return; }
  const uname = _socialProfile.username || 'perfil';
  const fname = code ? `metricas_${code}.csv` : `metricas_${uname}_${rows.length}posts.csv`;
  const blob = new Blob([_socialCsv(rows)], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = fname;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  showToast(`Métricas de ${rows.length} post(s) baixadas (CSV).`, 'success');
}

// ── Download de COMENTÁRIOS — um post (ícone no card) ou os selecionados ──
// Cada post é aberto no ego lite e os comentários vêm da própria rede; por isso
// leva alguns segundos por post e o backend limita a 25 por vez.
async function socialFetchComments(code, btnEl) {
  if (!_socialDatasetId) { showToast('Carregue uma coleta primeiro.', 'error'); return; }
  const codes = code ? [code] : [..._socialSel];
  if (!codes.length) { showToast('Selecione ao menos um item.', 'error'); return; }
  if (codes.length > 25) { showToast('Selecione no máximo 25 posts por vez.', 'error'); return; }

  const barBtn = document.getElementById('social-btn-comments');
  const prog = document.getElementById('social-progress');
  if (btnEl) btnEl.classList.add('loading');
  if (!code) { barBtn.disabled = true; barBtn.classList.add('loading'); }
  prog.style.display = '';
  prog.textContent = `Lendo comentários de ${codes.length} post(s)… isso abre o navegador, pode demorar.`;
  const done = () => {
    if (btnEl) btnEl.classList.remove('loading');
    barBtn.disabled = false; barBtn.classList.remove('loading');
    prog.style.display = 'none';
  };

  const fd = new FormData();
  fd.append('ds_id', _socialDatasetId);
  fd.append('codes', JSON.stringify(codes));
  try {
    const r = await fetch('/api/social/comments', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || 'falha');
    pollSocialJob(j.job_id, (res) => {
      done();
      if (!res) return;   // job falhou — toast já mostrado
      const a = document.createElement('a');
      a.href = '/api/social/export-file/' + encodeURIComponent(res.csv);
      a.download = res.csv; document.body.appendChild(a); a.click(); a.remove();
      const falhas = (res.failures || []).length;
      showToast(`${res.count} comentário(s) de ${res.posts} post(s) baixados (CSV)` +
                (falhas ? ` · ${falhas} post(s) sem comentários lidos` : '') + '.',
                falhas ? 'error' : 'success');
    });
  } catch (e) {
    done();
    showToast('Erro: ' + e.message, 'error');
  }
}

// ── Download rápido de UM post (ícone ⬇ no card), com spinner no botão ──
async function socialQuickDownload(code, btnEl) {
  if (btnEl) btnEl.classList.add('loading');
  const fd = new FormData();
  fd.append('ds_id', _socialDatasetId);
  fd.append('download_codes', JSON.stringify([code]));
  fd.append('transcribe_codes', '[]');
  const im = document.getElementById('social-include-meta');
  fd.append('include_meta', im && im.checked ? 'true' : 'false');
  showToast('Baixando mídia…');
  try {
    const r = await fetch('/api/social/fetch', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || 'falha');
    pollSocialJob(j.job_id, (res) => {
      if (btnEl) btnEl.classList.remove('loading');
      if (!res) return;
      showToast(res.downloaded ? 'Mídia baixada na Biblioteca.' : 'Nada baixado.',
                res.failed ? 'error' : 'success');
    });
  } catch (e) {
    if (btnEl) btnEl.classList.remove('loading');
    showToast('Erro: ' + e.message, 'error');
  }
}

// ── Ações em lote: mode 'media' (baixar) ou 'transcribe' (baixar + transcrever) ──
async function submitSocialFetch(mode) {
  const codes = [..._socialSel];
  if (!codes.length) { showToast('Selecione ao menos um item.', 'error'); return; }
  const byCode = new Map(_socialRows.map(r => [r.code, r]));
  let dl = [], tr = [];
  if (mode === 'transcribe') {
    tr = codes.filter(c => (byCode.get(c) || {}).is_video);
    dl = codes.filter(c => !(byCode.get(c) || {}).is_video);   // fotos só baixam
    if (!tr.length) { showToast('Nenhum vídeo selecionado para transcrever.', 'error'); return; }
  } else {
    dl = codes;
  }
  const fd = new FormData();
  fd.append('ds_id', _socialDatasetId);
  fd.append('download_codes', JSON.stringify(dl));
  fd.append('transcribe_codes', JSON.stringify(tr));
  fd.append('model', document.getElementById('social-model').value);
  fd.append('language', document.getElementById('social-language').value);
  const im = document.getElementById('social-include-meta');
  fd.append('include_meta', im && im.checked ? 'true' : 'false');
  const btns = ['social-btn-metrics', 'social-btn-comments', 'social-btn-download',
                'social-btn-transcribe'].map(id => document.getElementById(id));
  const clicked = document.getElementById(mode === 'transcribe' ? 'social-btn-transcribe' : 'social-btn-download');
  btns.forEach(b => b.disabled = true);
  clicked.classList.add('loading');
  const prog = document.getElementById('social-progress');
  prog.style.display = '';
  prog.textContent = mode === 'transcribe' ? 'Baixando e enviando p/ transcrição…' : 'Baixando mídia…';
  const done = () => { btns.forEach(b => b.disabled = false); clicked.classList.remove('loading'); prog.style.display = 'none'; };
  try {
    const r = await fetch('/api/social/fetch', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || 'falha');
    pollSocialJob(j.job_id, (res) => {
      done();
      if (!res) return;   // job falhou — toast já mostrado
      socialClearSel();
      let msg = `${res.downloaded} arquivo(s) baixado(s)`;
      if (res.transcribing) msg += `, ${res.transcribing} em transcrição`;
      if (res.failed) msg += `, ${res.failed} falha(s)`;
      showToast(msg + '.', res.failed ? 'error' : 'success');
      if (res.transcribing) setTimeout(() => switchMainTab('transcriptions'), 900);
      else setTimeout(() => switchMainTab('media'), 900);
    });
  } catch (e) {
    done();
    showToast('Erro: ' + e.message, 'error');
  }
}

// ── Insights / Tendências (calculados client-side, refletem os filtros) ──
let _socialInsightsOpen = false;

function toggleSocialInsights() {
  _socialInsightsOpen = !_socialInsightsOpen;
  document.getElementById('social-insights-toggle').setAttribute('aria-pressed', _socialInsightsOpen);
  document.getElementById('social-insights').style.display = _socialInsightsOpen ? '' : 'none';
  if (_socialInsightsOpen) renderSocialInsights();
}

function socialComputeTrends(rows) {
  const WD = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo'];
  const agg = (keyfn) => {
    const b = {};
    rows.forEach(r => {
      const k = keyfn(r); if (k === null || k === undefined) return;
      const e = b[k] || (b[k] = { n: 0, er: [], views: [] });
      e.n++; if (r.er != null) e.er.push(r.er); if (r.views) e.views.push(r.views);
    });
    return Object.entries(b).map(([k, e]) => ({
      key: k, posts: e.n,
      er: e.er.length ? +(e.er.reduce((a, c) => a + c, 0) / e.er.length).toFixed(2) : null,
      views: e.views.length ? Math.round(e.views.reduce((a, c) => a + c, 0) / e.views.length) : null,
    }));
  };
  const dOf = r => r.date ? new Date(r.date) : null;
  let weekday = agg(r => dOf(r) ? WD[(dOf(r).getDay() + 6) % 7] : null);
  weekday.sort((a, b) => WD.indexOf(a.key) - WD.indexOf(b.key));
  let hour = agg(r => dOf(r) ? dOf(r).getHours() : null).sort((a, b) => a.key - b.key);
  const durBucket = r => {
    const d = r.duration_s; if (!d) return null;
    if (d <= 15) return '0–15s'; if (d <= 30) return '15–30s';
    if (d <= 60) return '30–60s'; if (d <= 90) return '60–90s'; return '90s+';
  };
  const order = ['0–15s', '15–30s', '30–60s', '60–90s', '90s+'];
  let duration = agg(durBucket).sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
  let type = agg(r => r.type);
  const hb = {};
  rows.forEach(r => new Set(r.hashtags || []).forEach(h => {
    h = h.toLowerCase();
    const e = hb[h] || (hb[h] = { n: 0, er: [] });
    e.n++; if (r.er != null) e.er.push(r.er);
  }));
  let hashtag = Object.entries(hb).filter(([, e]) => e.n >= 2).map(([h, e]) => ({
    key: '#' + h, posts: e.n,
    er: e.er.length ? +(e.er.reduce((a, c) => a + c, 0) / e.er.length).toFixed(2) : 0,
  })).sort((a, b) => b.er - a.er).slice(0, 12);
  return { weekday, hour, type, duration, hashtag };
}

function socialBars(items, valKey, fmt) {
  if (!items.length) return '<div class="social-insight-empty">sem dados no filtro atual</div>';
  const max = Math.max(...items.map(i => i[valKey] || 0)) || 1;
  return items.map(i => {
    const v = i[valKey] || 0;
    const w = Math.max(2, Math.round(v / max * 100));
    return `<div class="social-bar-row"><span class="social-bar-label" title="${esc(String(i.key))}">${esc(String(i.key))}</span>` +
      `<span class="social-bar-track"><span class="social-bar-fill" style="width:${w}%"></span></span>` +
      `<span class="social-bar-val">${fmt(v)}</span></div>`;
  }).join('');
}

function renderSocialInsights() {
  if (!_socialInsightsOpen) return;
  const rows = socialFilteredRows();
  const t = socialComputeTrends(rows);
  const pct = v => v + '%';
  document.getElementById('social-insights').innerHTML =
    `<div class="social-insight-card"><h4>ER médio por dia da semana</h4>${socialBars(t.weekday, 'er', pct)}</div>` +
    `<div class="social-insight-card"><h4>Views médias por hora de postagem</h4>${socialBars(t.hour, 'views', socialFmtNum)}</div>` +
    `<div class="social-insight-card"><h4>ER médio por formato</h4>${socialBars(t.type, 'er', pct)}</div>` +
    `<div class="social-insight-card"><h4>ER médio por duração (reels)</h4>${socialBars(t.duration, 'er', pct)}</div>` +
    `<div class="social-insight-card social-insight-wide"><h4>${sic('hash')} Top hashtags por ER (mín. 2 posts)</h4>${socialBars(t.hashtag, 'er', pct)}</div>`;
}

async function socialExport() {
  if (!_socialDatasetId) { showToast('Carregue uma coleta primeiro.', 'error'); return; }
  const btn = document.getElementById('social-export-btn');
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = 'Gerando…';
  try {
    const fd = new FormData();
    fd.append('ds_id', _socialDatasetId);
    fd.append('sort', document.getElementById('social-sort').value);
    const r = await fetch('/api/social/export', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || 'falha');
    const a = document.createElement('a');
    a.href = '/api/social/export-file/' + encodeURIComponent(j.excel);
    a.download = j.excel; document.body.appendChild(a); a.click(); a.remove();
    showToast('Excel gerado' + (j.thumbs ? '' : ' (sem miniaturas — Pillow ausente)') + '.', 'success');
  } catch (e) {
    showToast('Erro ao exportar: ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

// ═══ Tema claro/escuro (padrão claro; escolha salva no localStorage) ═══
const _THEME_ICONS = {
  // Em tema CLARO mostramos a lua (clique = ir pro escuro)
  light: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
  // Em tema ESCURO mostramos o sol (clique = voltar pro claro)
  dark: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>',
};
function _currentTheme() {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}
function _updateThemeIcon() {
  const icon = document.getElementById('theme-icon');
  if (icon) icon.innerHTML = _THEME_ICONS[_currentTheme()];
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.title = _currentTheme() === 'dark' ? 'Mudar para tema claro' : 'Mudar para tema escuro';
}
function toggleTheme() {
  const next = _currentTheme() === 'dark' ? 'light' : 'dark';
  if (next === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
  else document.documentElement.removeAttribute('data-theme');   // sem atributo = claro (padrão)
  try { localStorage.setItem('whisper-theme', next); } catch (e) {}
  _updateThemeIcon();
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _updateThemeIcon);
else _updateThemeIcon();

// ═══════════════════════════════════════════════════════════════
//  ASSINATURAS — acompanha canais/perfis e traz o que sai de novo
// ═══════════════════════════════════════════════════════════════
// Toda esta seção é admin-only: o botão da aba tem .admin-only, switchMainTab
// bloqueia por _ADMIN_ONLY_TABS e o servidor recusa com 403. Três camadas.
let _subs = [];

const _SUBS_PLATFORM_LABEL = {
  youtube: 'YouTube', instagram: 'Instagram',
  tiktok: 'TikTok',   facebook: 'Facebook',
};

function _subsMsg(text, kind) {
  const el = document.getElementById('subs-msg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'pub-msg' + (text ? ' is-' + kind : '');
}

function _subsFmtWhen(ts) {
  if (!ts) return 'nunca';
  const diff = (Date.now() / 1000) - ts;
  if (diff < 60)    return 'agora há pouco';
  if (diff < 3600)  return `há ${Math.floor(diff / 60)} min`;
  if (diff < 86400) return `há ${Math.floor(diff / 3600)} h`;
  return `há ${Math.floor(diff / 86400)} d`;
}

async function loadSubscriptions() {
  try {
    const r = await fetch('/api/subscriptions');
    if (!r.ok) { _subsMsg('Não foi possível carregar as assinaturas.', 'error'); return; }
    const d = await r.json();
    _subs = d.subscriptions || [];
    renderSubscriptions();
  } catch {
    _subsMsg('Erro de rede ao carregar as assinaturas.', 'error');
  }
}

function renderSubscriptions() {
  const box = document.getElementById('subs-list');
  if (!box) return;
  if (!_subs.length) {
    box.innerHTML = `<p class="adv-dl-intro" style="margin-top:18px">
      Nenhuma assinatura ainda. Cadastre um canal acima para o sistema
      acompanhar sozinho.</p>`;
    return;
  }
  box.innerHTML = _subs.map(s => {
    const statusCls = s.last_status === 'erro' ? 'error'
                    : s.paused ? '' : 'success';
    return `
    <div class="subs-item${s.paused ? ' is-paused' : ''}">
      <div class="subs-item-main">
        <div class="subs-item-title">
          ${esc(s.label)}
          <span class="social-badge">${esc(_SUBS_PLATFORM_LABEL[s.platform] || s.platform)}</span>
          ${s.paused ? '<span class="social-badge">pausada</span>' : ''}
          ${s.auto_transcribe ? '<span class="social-badge">transcreve</span>'
                              : '<span class="social-badge">só baixa</span>'}
        </div>
        <div class="subs-item-sub">
          ${esc(s.target)} · a cada ${esc(String(s.interval_hours))}h ·
          até ${esc(String(s.max_per_check))} por checagem
          ${s.folder ? ` · pasta ${esc(s.folder)}` : ''}
        </div>
        <div class="subs-item-status ${statusCls}">
          Última checagem: ${esc(_subsFmtWhen(s.last_check_at))} — ${esc(s.last_message || '—')}
          ${s.total_fetched ? ` · ${esc(String(s.total_fetched))} baixados no total` : ''}
        </div>
      </div>
      <div class="subs-item-actions">
        <button type="button" class="btn" onclick="checkSubscriptionNow('${jsAttr(s.id)}')"
                title="Checar agora, sem esperar o intervalo">Checar agora</button>
        <button type="button" class="btn" onclick="toggleSubscription('${jsAttr(s.id)}', ${s.paused ? 'false' : 'true'})">
          ${s.paused ? 'Retomar' : 'Pausar'}
        </button>
        <button type="button" class="btn btn-danger" onclick="deleteSubscription('${jsAttr(s.id)}')"
                aria-label="Excluir assinatura ${esc(s.label)}">Excluir</button>
      </div>
    </div>`;
  }).join('');
}

async function addSubscription() {
  const platform = document.getElementById('subs-platform')?.value || 'youtube';
  const target   = (document.getElementById('subs-target')?.value || '').trim();
  if (!target) {
    _subsMsg('Informe o canal ou perfil que você quer acompanhar.', 'error');
    document.getElementById('subs-target')?.focus();
    return;
  }
  const fd = new FormData();
  fd.append('platform', platform);
  fd.append('target', target);
  fd.append('label',  document.getElementById('subs-label')?.value || '');
  fd.append('folder', document.getElementById('subs-folder')?.value || '');
  fd.append('model',    document.getElementById('subs-model')?.value || 'turbo');
  fd.append('language', document.getElementById('subs-language')?.value || 'pt');
  fd.append('interval_hours', document.getElementById('subs-interval')?.value || '6');
  fd.append('max_per_check',  document.getElementById('subs-max')?.value || '5');
  fd.append('initial_import', document.getElementById('subs-initial')?.value || '0');
  fd.append('auto_transcribe', document.getElementById('subs-auto')?.value || 'true');

  try {
    const r = await fetch('/api/subscriptions', { method: 'POST', body: fd });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { _subsMsg(d.detail || 'Não foi possível assinar.', 'error'); return; }
    document.getElementById('subs-target').value = '';
    document.getElementById('subs-label').value  = '';
    _subsMsg(`Assinatura criada. A primeira checagem acontece em instantes.`, 'ok');
    showToast('Assinatura criada.', 'success');
    await loadSubscriptions();
  } catch {
    _subsMsg('Erro de rede ao criar a assinatura.', 'error');
  }
}

async function toggleSubscription(id, paused) {
  const fd = new FormData();
  fd.append('paused', paused ? 'true' : 'false');
  try {
    const r = await fetch(`/api/subscriptions/${encodeURIComponent(id)}`, { method: 'POST', body: fd });
    if (!r.ok) { _subsMsg('Não foi possível alterar a assinatura.', 'error'); return; }
    await loadSubscriptions();
  } catch {
    _subsMsg('Erro de rede.', 'error');
  }
}

async function deleteSubscription(id) {
  const sub = _subs.find(s => s.id === id);
  const ok = await showConfirm({
    title: 'Excluir assinatura',
    message: `Parar de acompanhar "${sub?.label || id}"? O que já foi baixado continua no acervo.`,
    confirmText: 'Excluir',
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await fetch(`/api/subscriptions/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (!r.ok) { _subsMsg('Não foi possível excluir.', 'error'); return; }
    showToast('Assinatura removida.', 'success');
    await loadSubscriptions();
  } catch {
    _subsMsg('Erro de rede.', 'error');
  }
}

async function checkSubscriptionNow(id) {
  _subsMsg('Checando… nas redes sociais isso abre o ego lite e pode demorar um pouco.', 'ok');
  try {
    const r = await fetch(`/api/subscriptions/${encodeURIComponent(id)}/check`, { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { _subsMsg(d.detail || 'Não foi possível checar.', 'error'); return; }
    // A checagem roda em thread no servidor: recarrega em alguns segundos para
    // mostrar o resultado sem obrigar o usuário a atualizar a página.
    setTimeout(loadSubscriptions, 4000);
    setTimeout(loadSubscriptions, 12000);
  } catch {
    _subsMsg('Erro de rede.', 'error');
  }
}

// ═══════════════════════════════════════════════════════════════
//  COMPRESSÃO DE MÍDIA — reduz o tamanho sem perder o arquivo
// ═══════════════════════════════════════════════════════════════
// Admin-only: reescreve arquivos do acervo (inclusive privados) e, com
// "substituir", é irreversível para quem só tinha aquela cópia.
const _COMPRESS_PRESET_KEY = 'wt:compress-preset';

async function compressSelectedMedia() {
  const files = Array.from(_mediaSelected);
  if (!files.length) return;

  // Só faz sentido para áudio/vídeo — nunca some com a seleção em silêncio.
  const alvos = files.filter(f => {
    const t = _fileTypeFor(f);
    return t === 'video' || t === 'audio';
  });
  if (!alvos.length) {
    showToast('Nenhum arquivo de áudio ou vídeo na seleção.', 'error');
    return;
  }

  let caps;
  try {
    caps = await (await fetch('/api/compress/capabilities')).json();
  } catch {
    showToast('Não foi possível falar com o servidor.', 'error');
    return;
  }
  if (!caps.available) {
    await showAlert({
      title: 'FFmpeg não encontrado',
      message: 'A compressão precisa do FFmpeg instalado nesta máquina. '
             + 'Instale com "brew install ffmpeg" e tente de novo.',
    });
    return;
  }

  // Estimativa real do primeiro arquivo — dá ao usuário uma noção concreta do
  // ganho antes de ele confirmar uma operação que reescreve arquivos.
  let previa = '';
  try {
    const saved = localStorage.getItem(_COMPRESS_PRESET_KEY) || 'medio';
    const r = await fetch(`/api/compress/plan/${encodeURIComponent(alvos[0])}?preset=${encodeURIComponent(saved)}`);
    if (r.ok) {
      const p = await r.json();
      previa = p.worth_it
        ? `\n\nExemplo (${alvos[0]}): ${_formatBytes(p.size_bytes)} → cerca de `
          + `${_formatBytes(p.estimated_bytes)} (~${p.estimated_saving_pct}% menor).`
        : `\n\nObservação: "${alvos[0]}" já está compacto e será mantido como está.`;
    }
  } catch { /* estimativa é um extra — segue sem ela */ }

  const preset = await showChoice({
    title: `Comprimir ${alvos.length} ${alvos.length === 1 ? 'arquivo' : 'arquivos'}`,
    message: 'Escolha o nível. O arquivo original é substituído pelo comprimido — '
           + 'quem já tinha baixado não é afetado, mas no servidor fica só a versão nova.'
           + (caps.hw_h264 ? ' Este Mac comprime por hardware, então costuma ser rápido.' : '')
           + previa,
    choices: (caps.presets || []).map(p => ({
      value: p.id,
      label: p.label,
      danger: p.id === 'forte',
    })),
  });
  if (!preset) return;

  try { localStorage.setItem(_COMPRESS_PRESET_KEY, preset); } catch {}

  const fd = new FormData();
  fd.append('files', alvos.join(','));
  fd.append('preset', preset);
  fd.append('replace', 'true');
  try {
    const r = await fetch('/api/compress', { method: 'POST', body: fd });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { showToast(d.detail || 'Não foi possível comprimir.', 'error'); return; }
    showToast(`Comprimindo ${d.count} ${d.count === 1 ? 'arquivo' : 'arquivos'}…`, 'success');
    // Cada arquivo tem sua própria task — acompanha todas.
    (d.started || []).forEach(s => pollCompression(s.task_id, s.file));
  } catch {
    showToast('Erro de rede ao iniciar a compressão.', 'error');
  }
}

function pollCompression(taskId, filename) {
  if (_activePolls[taskId]) return;
  _activePolls[taskId] = setInterval(async () => {
    try {
      const res = await fetch(`/api/progress/${taskId}`);
      if (!res.ok) { _stopPoll(taskId); return; }
      const d = await res.json();
      _updateRowStatus(filename, d.status, d.progress || 0, d.phase, d.phase_progress);

      if (d.status === 'done') {
        _stopPoll(taskId);
        if (d.skipped) {
          showToast(`"${filename}": ${d.message || 'mantido como estava'}.`, '');
        } else {
          showToast(`"${filename}" comprimido — ${_formatBytes(d.saved_bytes || 0)} liberados `
                  + `(${d.saved_pct}% menor).`, 'success');
        }
        if (typeof loadMedia === 'function') loadMedia();
        loadStats();
      } else if (d.status === 'error') {
        _stopPoll(taskId);
        showToast(`Falha ao comprimir "${filename}": ${d.error || 'erro'}`, 'error');
        if (typeof loadMedia === 'function') loadMedia();
      } else if (d.status === 'cancelled') {
        _stopPoll(taskId);
        if (typeof loadMedia === 'function') loadMedia();
      }
    } catch { /* blip de rede */ }
  }, 900);
}
