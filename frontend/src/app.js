/**
 * oAIo — 4-view dashboard
 */

// ── XSS Prevention ──────────────────────────────────────────────────────────
function _esc(s) {
  if (s == null) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

// ── Auth ─────────────────────────────────────────────────────────────────────
let _apiToken = localStorage.getItem('oaio-api-token') || '';

async function _fetch(url, opts = {}) {
  opts.headers = opts.headers || {};
  if (_apiToken) opts.headers['Authorization'] = 'Bearer ' + _apiToken;
  const r = await fetch(url, opts);
  if (r.status === 401) {
    const token = prompt('API token required:');
    if (token) {
      _apiToken = token.trim();
      localStorage.setItem('oaio-api-token', _apiToken);
      opts.headers['Authorization'] = 'Bearer ' + _apiToken;
      return fetch(url, opts);
    }
  }
  return r;
}

// ── State ────────────────────────────────────────────────────────────────────
let ws = null;
let wsRetry = 1000;
let lastWsData = {};
let _startTime = Date.now();

// ── Tab Switching ────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    const tab = document.getElementById('tab-' + btn.dataset.tab);
    if (tab) tab.classList.add('active');
    if (btn.dataset.tab === 'build') refreshBuild();
    if (btn.dataset.tab === 'settings') refreshSettings();
  });
});

// ── WebSocket ────────────────────────────────────────────────────────────────
function wsConnect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let url = `${proto}//${location.host}/ws`;
  if (_apiToken) url += `?token=${encodeURIComponent(_apiToken)}`;
  ws = new WebSocket(url);
  ws.onopen = () => {
    wsRetry = 1000;
    document.getElementById('ws-banner').classList.add('hidden');
  };
  ws.onclose = () => {
    document.getElementById('ws-banner').classList.remove('hidden');
    setTimeout(wsConnect, Math.min(wsRetry, 30000));
    wsRetry = Math.min(wsRetry * 1.5, 30000);
  };
  ws.onmessage = (e) => {
    try {
      lastWsData = JSON.parse(e.data);
      updateLive(lastWsData);
    } catch(err) { console.error('WS parse error:', err); }
  };
}

// ── LIVE View ────────────────────────────────────────────────────────────────
function updateLive(d) {
  // Gauges
  updateGauge('vram', d.vram?.used_gb, d.vram?.total_gb, `${(d.vram?.used_gb||0).toFixed(1)} / ${(d.vram?.total_gb||20).toFixed(0)} GB`);
  updateGauge('ram', d.ram?.used_gb, d.ram?.total_gb, `${(d.ram?.used_gb||0).toFixed(1)} / ${(d.ram?.total_gb||62).toFixed(0)} GB`);
  updateGaugePercent('gpu', d.gpu?.gpu_use_percent||0, `${Math.round(d.gpu?.gpu_use_percent||0)}%`);

  // Active mode badge
  const modes = d.active_modes || [];
  document.getElementById('active-mode-badge').textContent = modes.length ? modes.join(', ').toUpperCase() : 'NO MODE';

  // Modes list
  renderModes(d);

  // Services list
  renderServices(d);

  // VRAM breakdown
  renderVramBreakdown(d);

  // Kill log
  renderKillLog(d.kill_log || []);

  // Companion clients
  refreshClients();

  // Enforcement
  renderEnforcement(d.enforcement || {});

  // About
  const svcCount = Object.keys(d.services || {}).length;
  const el = document.getElementById('about-services');
  if (el) el.textContent = `Services: ${svcCount} registered`;
  const up = document.getElementById('about-uptime');
  if (up) up.textContent = `Session: ${Math.round((Date.now() - _startTime) / 60000)} min`;
}

function updateGauge(id, used, total, label) {
  const pct = total > 0 ? (used / total * 100) : 0;
  const bar = document.getElementById(id + '-bar');
  bar.style.width = pct + '%';
  bar.className = 'fill' + (pct > 85 ? ' crit' : pct > 70 ? ' warn' : '');
  document.getElementById(id + '-label').textContent = label;
}

function updateGaugePercent(id, pct, label) {
  const bar = document.getElementById(id + '-bar');
  bar.style.width = pct + '%';
  bar.className = 'fill' + (pct > 85 ? ' crit' : pct > 70 ? ' warn' : '');
  document.getElementById(id + '-label').textContent = label;
}

function renderModes(d) {
  const el = document.getElementById('modes-list');
  const modesData = d._modes || {};
  const activeModes = new Set(d.active_modes || []);

  // Fetch modes on first render
  if (!d._modes && !d._modesFetching) {
    d._modesFetching = true;
    _fetch('/modes').then(r => r.json()).then(m => {
      d._modes = m;
      renderModes(d);
    }).catch(() => {});
    return;
  }
  if (!d._modes) return;

  let html = '';
  for (const [key, mode] of Object.entries(modesData)) {
    const isActive = activeModes.has(key);
    const name = _esc(mode.name || key);
    const budget = mode.vram_budget_gb || 0;
    const svcList = (mode.services || []).map(s => _esc(s)).join(', ');
    html += `<div class="item-row clickable" data-mode="${_esc(key)}">
      <div class="item-info">
        <div class="status-dot ${isActive ? 'active' : 'exited'}"></div>
        <div>
          <div class="item-name">${name}</div>
          <div class="item-detail">${svcList} &middot; ${budget}GB</div>
        </div>
      </div>
      <div class="item-actions">
        ${isActive ? '<span class="badge badge-active">ACTIVE</span>' : ''}
        <span class="badge badge-vram">${budget}G</span>
      </div>
    </div>`;
  }
  el.innerHTML = html || '<div class="item-detail">No modes configured</div>';

  // Click handlers
  el.querySelectorAll('[data-mode]').forEach(row => {
    row.addEventListener('click', () => toggleMode(row.dataset.mode));
  });
}

async function toggleMode(key) {
  const active = lastWsData.active_modes || [];
  if (active.includes(key)) {
    if (!confirm(`Deactivate mode "${key}"?`)) return;
    await _fetch(`/modes/${encodeURIComponent(key)}/deactivate`, { method: 'POST' });
  } else {
    // Check VRAM first
    const checkR = await _fetch(`/modes/${encodeURIComponent(key)}/check`);
    const check = await checkR.json();
    const msg = check.blocked
      ? `VRAM BLOCKED: ${check.warning || 'Budget exceeded'}. Force activate?`
      : `Activate "${key}"? Projected: ${check.projected_gb?.toFixed(1) || '?'}GB / ${check.budget_gb || '?'}GB`;
    if (!confirm(msg)) return;
    await _fetch(`/modes/${encodeURIComponent(key)}/activate${check.blocked ? '?force=true' : ''}`, { method: 'POST' });
  }
}

function renderServices(d) {
  const el = document.getElementById('services-list');
  // services comes as a list [{name, status, ...}] from the WS, not a dict
  const services = Array.isArray(d.services) ? d.services : Object.values(d.services || {});
  let html = '';
  for (const svc of services) {
    const name = svc.name || '?';
    const status = svc.status || 'unknown';
    const dotClass = status === 'running' ? 'running' : status === 'exited' ? 'exited' : 'error';
    const vram = svc.vram_mb ? (svc.vram_mb / 1024).toFixed(1) + 'G' : '';
    html += `<div class="item-row">
      <div class="item-info">
        <div class="status-dot ${dotClass}"></div>
        <div>
          <div class="item-name">${_esc(name)}</div>
          <div class="item-detail">${_esc(status)}${vram ? ' &middot; ' + _esc(vram) : ''}</div>
        </div>
      </div>
      <div class="item-actions">
        ${vram ? '<span class="badge badge-vram">' + _esc(vram) + '</span>' : ''}
        <button class="btn-sm" onclick="toggleService('${_esc(name)}', '${_esc(status)}')">${status === 'running' ? 'Stop' : 'Start'}</button>
      </div>
    </div>`;
  }
  el.innerHTML = html || '<div class="item-detail">No services</div>';
}

async function toggleService(name, currentStatus) {
  const action = currentStatus === 'running' ? 'stop' : 'start';
  await _fetch(`/services/${encodeURIComponent(name)}/${action}`, { method: 'POST' });
}

function renderVramBreakdown(d) {
  const el = document.getElementById('vram-breakdown');
  const total = d.vram?.total_gb || 20;
  const services = d.services || {};
  let html = '';
  for (const [name, svc] of Object.entries(services)) {
    if (svc.status !== 'running') continue;
    const vramGb = (svc.vram_mb || 0) / 1024;
    if (vramGb < 0.01) continue;
    const pct = (vramGb / total * 100).toFixed(1);
    html += `<div class="vram-svc-bar">
      <span class="svc-name">${_esc(name)}</span>
      <div class="bar"><div class="fill" style="width:${pct}%"></div></div>
      <span class="svc-val">${vramGb.toFixed(1)}G</span>
    </div>`;
  }
  // Ollama loaded models
  for (const m of (d.ollama_loaded || [])) {
    const gb = (m.size_vram || 0) / 1e9;
    if (gb < 0.01) continue;
    const pct = (gb / total * 100).toFixed(1);
    html += `<div class="vram-svc-bar">
      <span class="svc-name">${_esc(m.name)}</span>
      <div class="bar"><div class="fill" style="width:${pct}%"></div></div>
      <span class="svc-val">${gb.toFixed(1)}G</span>
    </div>`;
  }
  el.innerHTML = html || '<div class="item-detail">No VRAM usage</div>';
}

function renderKillLog(log) {
  const el = document.getElementById('kill-log');
  if (!log.length) { el.innerHTML = '<div class="item-detail">No enforcer events</div>'; return; }
  el.innerHTML = log.slice(0, 10).map(e =>
    `<div class="log-entry">${_esc(e.ts || '')} ${_esc(e.service || '')} killed (pri ${_esc(e.priority)})</div>`
  ).join('');
}

let _clientsTimer = 0;
async function refreshClients() {
  if (++_clientsTimer % 5 !== 1) return; // every 5 WS ticks
  const el = document.getElementById('companion-clients');
  try {
    const r = await _fetch('/extensions/companion/clients');
    const clients = await r.json();
    document.getElementById('clients-count').textContent = clients.length + ' client' + (clients.length !== 1 ? 's' : '');
    if (!clients.length) { el.innerHTML = '<div class="item-detail">No clients connected</div>'; return; }
    el.innerHTML = clients.map(c => `<div class="item-row">
      <div class="item-info">
        <div class="status-dot running"></div>
        <div>
          <div class="item-name">${_esc(c.info?.name || c.id)}</div>
          <div class="item-detail">${_esc(c.info?.platform || '?')} &middot; ${Math.round(c.uptime_s / 60)}m</div>
        </div>
      </div>
    </div>`).join('');
  } catch(e) { el.innerHTML = '<div class="item-detail">Error loading clients</div>'; }
}

function renderEnforcement(enf) {
  const el = document.getElementById('enforcement-status');
  el.innerHTML = `<div class="item-row">
    <div class="item-info"><div class="item-name">Status</div></div>
    <div class="item-detail">${enf.enabled ? (enf.enforcing ? '<span style="color:var(--red)">ENFORCING</span>' : 'Enabled') : '<span style="color:var(--text-muted)">Disabled</span>'}</div>
  </div>
  <div class="item-row">
    <div class="item-info"><div class="item-name">Mode</div></div>
    <div class="item-detail">${_esc(enf.mode || 'estimated')}</div>
  </div>
  <div class="item-row">
    <div class="item-info"><div class="item-name">Thresholds</div></div>
    <div class="item-detail">Warn ${((enf.warn_threshold||0.7)*100).toFixed(0)}% &middot; Hard ${((enf.hard_threshold||0.85)*100).toFixed(0)}%</div>
  </div>
  ${enf.vram_ceiling_gb ? `<div class="item-row"><div class="item-info"><div class="item-name">VRAM Ceiling</div></div><div class="item-detail">${enf.vram_ceiling_gb}GB</div></div>` : ''}`;
}

// ── BUILD View ───────────────────────────────────────────────────────────────
async function refreshBuild() {
  // Services
  try {
    const r = await _fetch('/config/services');
    const services = await r.json();
    const el = document.getElementById('build-services');
    let html = '';
    for (const [name, svc] of Object.entries(services)) {
      html += `<div class="item-row">
        <div class="item-info">
          <div>
            <div class="item-name">${_esc(name)}</div>
            <div class="item-detail">${_esc(svc.container || name)} :${svc.port || '?'} &middot; ${_esc(svc.group || 'Other')} &middot; VRAM ${svc.vram_est_gb || 0}G &middot; pri ${svc.priority || 3}</div>
          </div>
        </div>
        <div class="item-actions">
          <button class="btn-sm" onclick="scanService('${_esc(name)}')">Scan</button>
        </div>
      </div>`;
    }
    el.innerHTML = html || '<div class="item-detail">No services registered</div>';
  } catch(e) { console.error('Build services error:', e); }

  // Modes
  try {
    const r = await _fetch('/modes');
    const modes = await r.json();
    const el = document.getElementById('build-modes');
    let html = '';
    for (const [key, mode] of Object.entries(modes)) {
      const svcList = (mode.services || []).map(s => _esc(s)).join(', ');
      html += `<div class="item-row">
        <div class="item-info">
          <div>
            <div class="item-name">${_esc(mode.name || key)}</div>
            <div class="item-detail">${svcList} &middot; Budget: ${mode.vram_budget_gb || 0}GB</div>
          </div>
        </div>
        <div class="item-actions">
          <button class="btn-sm" onclick="deleteMode('${_esc(key)}')">Delete</button>
        </div>
      </div>`;
    }
    el.innerHTML = html || '<div class="item-detail">No modes configured</div>';
  } catch(e) { console.error('Build modes error:', e); }
}

async function scanService(name) {
  alert('Scanning ' + name + '...');
  try {
    const r = await _fetch(`/services/${encodeURIComponent(name)}/scan`, { method: 'POST' });
    const result = await r.json();
    alert(`Scan complete: ${result.endpoints?.length || 0} endpoints, caps: ${(result.capabilities || []).join(', ')}`);
  } catch(e) { alert('Scan failed: ' + e.message); }
}

async function deleteMode(key) {
  if (!confirm(`Delete mode "${key}"?`)) return;
  await _fetch(`/modes/${encodeURIComponent(key)}`, { method: 'DELETE' });
  refreshBuild();
}

// ── Add Service Modal ────────────────────────────────────────────────────────
document.getElementById('add-service-btn')?.addEventListener('click', () => {
  openModal('Add Service', `
    <div class="form-group"><label>Name</label><input id="m-svc-name" placeholder="my-service" /></div>
    <div class="form-group"><label>Container</label><input id="m-svc-container" placeholder="container-name" /></div>
    <div class="form-group"><label>Port</label><input id="m-svc-port" type="number" value="8000" /></div>
    <div class="form-group"><label>VRAM (GB)</label><input id="m-svc-vram" type="number" step="0.5" value="0" /></div>
    <div class="form-group"><label>RAM (GB)</label><input id="m-svc-ram" type="number" step="0.1" value="0" /></div>
    <div class="form-group"><label>Priority (1=high, 50=low)</label><input id="m-svc-priority" type="number" value="20" /></div>
    <div class="form-group"><label>Group</label><input id="m-svc-group" value="Other" /></div>
    <div class="form-group"><label>Description</label><input id="m-svc-desc" /></div>
  `, async () => {
    const body = {
      name: document.getElementById('m-svc-name').value,
      container: document.getElementById('m-svc-container').value || document.getElementById('m-svc-name').value,
      port: parseInt(document.getElementById('m-svc-port').value) || 8000,
      vram_est_gb: parseFloat(document.getElementById('m-svc-vram').value) || 0,
      ram_est_gb: parseFloat(document.getElementById('m-svc-ram').value) || 0,
      priority: parseInt(document.getElementById('m-svc-priority').value) || 20,
      group: document.getElementById('m-svc-group').value || 'Other',
      description: document.getElementById('m-svc-desc').value || '',
    };
    await _fetch('/config/services', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
    closeModal();
    refreshBuild();
  });
});

// ── Add Mode Modal ───────────────────────────────────────────────────────────
document.getElementById('add-mode-btn')?.addEventListener('click', async () => {
  let svcOpts = '';
  try {
    const r = await _fetch('/config/services');
    const svcs = await r.json();
    for (const name of Object.keys(svcs)) {
      svcOpts += `<label style="display:block;margin:2px 0;font-size:12px;color:var(--text)"><input type="checkbox" class="m-mode-svc" value="${_esc(name)}" /> ${_esc(name)}</label>`;
    }
  } catch(e) {}
  openModal('Add Mode', `
    <div class="form-group"><label>Name</label><input id="m-mode-name" placeholder="my-mode" /></div>
    <div class="form-group"><label>Description</label><input id="m-mode-desc" /></div>
    <div class="form-group"><label>VRAM Budget (GB)</label><input id="m-mode-budget" type="number" step="0.5" value="10" /></div>
    <div class="form-group"><label>Services</label><div>${svcOpts || 'No services'}</div></div>
  `, async () => {
    const svcs = [...document.querySelectorAll('.m-mode-svc:checked')].map(c => c.value);
    const body = {
      name: document.getElementById('m-mode-name').value,
      description: document.getElementById('m-mode-desc').value || '',
      vram_budget_gb: parseFloat(document.getElementById('m-mode-budget').value) || 10,
      services: svcs,
    };
    await _fetch('/modes', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
    closeModal();
    refreshBuild();
  });
});

// ── SETTINGS View ────────────────────────────────────────────────────────────
async function refreshSettings() {
  // Load companion config
  try {
    const r = await _fetch('/extensions/companion/config');
    const cfg = await r.json();
    document.getElementById('set-voice').value = cfg.tts_voice || 'af_heart';
    // Resource preset
    const presetSel = document.getElementById('set-preset');
    if (presetSel && cfg.resource_preset) {
      presetSel.value = cfg.resource_preset;
    }
    document.getElementById('set-prompt').value = cfg.system_prompt || '';
    const engineSel = document.getElementById('set-tts-engine');
    for (let i = 0; i < engineSel.options.length; i++) {
      if (engineSel.options[i].value === (cfg.tts_engine || 'kokoro')) engineSel.selectedIndex = i;
    }
    // Persona
    const priorityEl = document.getElementById('set-priority');
    if (priorityEl) {
      priorityEl.value = cfg.persona_priority ?? 3;
      document.getElementById('set-priority-val').textContent = priorityEl.value;
      priorityEl.addEventListener('input', () => {
        document.getElementById('set-priority-val').textContent = priorityEl.value;
      });
    }
    // RAG toggles
    const ragMap = {
      'set-rag-knowledge': 'rag_knowledge_enabled',
      'set-rag-web': 'rag_web_search_enabled',
      'set-rag-notes': 'rag_notes_enabled',
      'set-rag-episodes': 'rag_episodes_enabled',
      'set-rag-vision': 'rag_vision_memory_enabled',
      'set-rag-git': 'rag_git_enabled',
    };
    for (const [elId, cfgKey] of Object.entries(ragMap)) {
      const el = document.getElementById(elId);
      if (el) el.checked = cfg[cfgKey] !== false;  // default true except git
    }
    if (document.getElementById('set-rag-git')) {
      document.getElementById('set-rag-git').checked = cfg.rag_git_enabled === true;  // default false
    }
    // Other fields
    if (document.getElementById('set-git-path')) document.getElementById('set-git-path').value = cfg.git_repo_path || '';
    if (document.getElementById('set-num-ctx')) document.getElementById('set-num-ctx').value = cfg.llm_num_ctx || 4096;
    if (document.getElementById('set-temperature')) document.getElementById('set-temperature').value = cfg.llm_temperature ?? '';
    if (document.getElementById('set-rag-temp')) document.getElementById('set-rag-temp').value = cfg.rag_temperature ?? 0.2;
    // Store cfg for vision model population later
    window._companionCfg = cfg;
  } catch(e) {}

  // Load ollama models
  try {
    const r = await _fetch('/services/ollama/models');
    const models = await r.json();
    const sel = document.getElementById('set-model');
    sel.innerHTML = '';
    if (Array.isArray(models)) {
      models.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.name;
        opt.textContent = `${m.name} (${m.size_gb}GB)`;
        sel.appendChild(opt);
      });
    }
    // Populate vision model dropdown with same list
    const visSel = document.getElementById('set-vision-model');
    if (visSel) {
      visSel.innerHTML = '<option value="">(none)</option>';
      if (Array.isArray(models)) {
        models.forEach(m => {
          const opt = document.createElement('option');
          opt.value = m.name;
          opt.textContent = `${m.name} (${m.size_gb}GB)`;
          visSel.appendChild(opt);
        });
      }
      const savedVision = window._companionCfg?.vision_model || '';
      visSel.value = savedVision;
    }
  } catch(e) {}

  // Load storage stats
  try {
    const r = await _fetch('/config/storage/stats');
    const stats = await r.json();
    const el = document.getElementById('storage-stats');
    let html = '';
    for (const [mount, info] of Object.entries(stats)) {
      const pct = info.percent || 0;
      const color = pct > 85 ? 'var(--red)' : pct > 70 ? 'var(--yellow)' : 'var(--green)';
      html += `<div class="storage-drive">
        <span style="min-width:120px;font-size:11px">${_esc(mount)}</span>
        <div class="bar"><div class="fill" style="width:${pct}%;background:${color}"></div></div>
        <span style="font-size:11px;color:var(--text-dim);min-width:80px;text-align:right">${(info.used_gb||0).toFixed(0)}/${(info.total_gb||0).toFixed(0)}GB</span>
      </div>`;
    }
    el.innerHTML = html || '<div class="item-detail">No storage info</div>';
  } catch(e) {}

  // Load symlinks
  try {
    const r = await _fetch('/config/paths');
    const paths = await r.json();
    const el = document.getElementById('symlink-table');
    let html = '';
    if (Array.isArray(paths)) {
      paths.forEach(p => {
        const name = _esc(p.name || p.label || '?');
        const ok = p.exists !== false;
        const tier = _esc(p.tier || '');
        const isRam = tier === 'ram';
        html += `<div class="symlink-row">
          <span class="symlink-name">${name}</span>
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis">${_esc(p.target || '')}</span>
          <span style="color:${ok ? 'var(--green)' : 'var(--red)'}; min-width:50px">${ok ? 'OK' : 'BROKEN'}</span>
          <button class="btn-sm${isRam ? ' active' : ''}" onclick="repointSymlink('${name}', '${isRam ? 'default' : 'ram'}')">${isRam ? 'SSD' : 'RAM'}</button>
          <button class="btn-sm" onclick="repointSymlinkCustom('${name}')">Repoint</button>
        </div>`;
      });
    }
    el.innerHTML = html || '<div class="item-detail">No symlinks</div>';
  } catch(e) {}

  // Load enforcement state
  if (lastWsData.enforcement) {
    const enf = lastWsData.enforcement;
    const enfToggle = document.getElementById('enforcer-toggle');
    enfToggle.textContent = enf.enabled ? 'Enabled' : 'Disabled';
    enfToggle.className = 'btn-toggle' + (enf.enabled ? '' : ' off');

    const modeSel = document.getElementById('enforcement-mode-select');
    modeSel.value = enf.mode || 'estimated';

    document.getElementById('vram-ceiling').value = enf.vram_ceiling_gb || 0;
  }

  // Boot config
  try {
    const r = await _fetch('/config/boot');
    const boot = await r.json();
    const btn = document.getElementById('boot-toggle');
    btn.textContent = boot.enabled ? 'Enabled' : 'Disabled';
    btn.className = 'btn-toggle' + (boot.enabled ? '' : ' off');
  } catch(e) {}
}

// Save companion
document.getElementById('save-companion')?.addEventListener('click', async () => {
  const body = {
    ollama_model: document.getElementById('set-model').value,
    tts_voice: document.getElementById('set-voice').value,
    tts_engine: document.getElementById('set-tts-engine').value,
    system_prompt: document.getElementById('set-prompt').value,
    // Persona
    vision_model: document.getElementById('set-vision-model')?.value || undefined,
    // RAG toggles
    rag_knowledge_enabled: document.getElementById('set-rag-knowledge')?.checked ?? true,
    rag_web_search_enabled: document.getElementById('set-rag-web')?.checked ?? true,
    rag_notes_enabled: document.getElementById('set-rag-notes')?.checked ?? true,
    rag_episodes_enabled: document.getElementById('set-rag-episodes')?.checked ?? true,
    rag_vision_memory_enabled: document.getElementById('set-rag-vision')?.checked ?? true,
    rag_git_enabled: document.getElementById('set-rag-git')?.checked ?? false,
    git_repo_path: document.getElementById('set-git-path')?.value || '',
    // LLM tuning
    llm_num_ctx: parseInt(document.getElementById('set-num-ctx')?.value) || 4096,
    rag_temperature: parseFloat(document.getElementById('set-rag-temp')?.value) || 0.2,
  };
  // Only include temperature if explicitly set
  const tempVal = document.getElementById('set-temperature')?.value;
  if (tempVal !== '' && tempVal != null) body.llm_temperature = parseFloat(tempVal);
  await _fetch('/extensions/companion/config', { method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
  // Save priority separately via persona endpoint
  const priority = parseInt(document.getElementById('set-priority')?.value);
  if (!isNaN(priority)) {
    await _fetch('/extensions/companion/persona/priority', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ priority }),
    });
  }
  alert('Companion settings saved');
});

// Apply resource preset
document.getElementById('apply-preset')?.addEventListener('click', async () => {
  const preset = document.getElementById('set-preset').value;
  if (!preset) return;
  const btn = document.getElementById('apply-preset');
  btn.textContent = 'Applying...';
  btn.disabled = true;
  try {
    const r = await _fetch('/extensions/companion/presets/' + preset, { method: 'POST' });
    const result = await r.json();
    if (result.error) {
      alert('Preset error: ' + result.error);
    } else {
      alert('Preset "' + preset + '" applied.\nModels loaded: ' + (result.models_loaded || []).join(', ') +
            '\nModels unloaded: ' + (result.models_unloaded || []).join(', '));
      refreshSettings();
    }
  } catch(e) { alert('Failed to apply preset'); }
  btn.textContent = 'Apply';
  btn.disabled = false;
});

// Save system
document.getElementById('save-system')?.addEventListener('click', async () => {
  const ceiling = parseFloat(document.getElementById('vram-ceiling').value) || 0;
  await _fetch('/enforcement/ceiling', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ vram_ceiling_gb: ceiling || null }),
  });
  const mode = document.getElementById('enforcement-mode-select').value;
  await _fetch('/enforcement/mode', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ mode }),
  });
  alert('System settings saved');
});

// Enforcer toggle
document.getElementById('enforcer-toggle')?.addEventListener('click', async function() {
  const isEnabled = !this.classList.contains('off');
  await _fetch(`/enforcement/${isEnabled ? 'disable' : 'enable'}`, { method: 'POST' });
  this.classList.toggle('off');
  this.textContent = isEnabled ? 'Disabled' : 'Enabled';
});

// Boot toggle
document.getElementById('boot-toggle')?.addEventListener('click', async function() {
  const isEnabled = !this.classList.contains('off');
  await _fetch('/config/boot', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ enabled: !isEnabled }),
  });
  this.classList.toggle('off');
  this.textContent = isEnabled ? 'Disabled' : 'Enabled';
});

// ── Symlink Repoint ──────────────────────────────────────────────────────────
async function repointSymlink(name, target) {
  try {
    const r = await _fetch(`/config/paths/${encodeURIComponent(name)}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ target }),
    });
    const result = await r.json();
    if (result.error) {
      alert('Repoint failed: ' + result.error);
    } else {
      refreshSettings();
    }
  } catch(e) { alert('Repoint error: ' + e.message); }
}

async function repointSymlinkCustom(name) {
  const target = prompt(`New target path for "${name}":\n(Enter absolute path, "ram", or "default")`);
  if (!target) return;
  await repointSymlink(name, target.trim());
}

// ── Emergency Kill ───────────────────────────────────────────────────────────
document.getElementById('emergency-kill')?.addEventListener('click', async () => {
  if (!confirm('EMERGENCY KILL — Stop ALL services and clear all modes?')) return;
  await _fetch('/emergency/kill', { method: 'POST' });
});

// ── Modal ────────────────────────────────────────────────────────────────────
let _modalCallback = null;
function openModal(title, bodyHtml, onConfirm) {
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-body').innerHTML = bodyHtml;
  document.getElementById('modal-overlay').classList.remove('hidden');
  _modalCallback = onConfirm;
}
function closeModal() {
  document.getElementById('modal-overlay').classList.add('hidden');
  _modalCallback = null;
}
document.getElementById('modal-close')?.addEventListener('click', closeModal);
document.getElementById('modal-cancel')?.addEventListener('click', closeModal);
document.getElementById('modal-confirm')?.addEventListener('click', () => { if (_modalCallback) _modalCallback(); });
document.getElementById('modal-overlay')?.addEventListener('click', (e) => { if (e.target.id === 'modal-overlay') closeModal(); });

// ── Init ─────────────────────────────────────────────────────────────────────
wsConnect();
