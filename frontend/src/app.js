/**
 * oAIo — main app
 */

// Escape HTML special chars to prevent injection when interpolating user data into innerHTML
function _esc(s) {
  if (s == null) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

// OLLMO_API defined globally in index.html

// --- Visible JS error reporting ---
window.onerror = (msg, src, line) => {
  const el = document.getElementById("alert-banner");
  if (el) {
    el.textContent = `JS error: ${msg} (${(src||'').split('/').pop()}:${line})`;
    el.className = "alert-banner critical";
    el.classList.remove("hidden");
  }
};

// --- Litegraph setup (lazy — scripts + init deferred until CONFIG tab) ---
let graph     = null;
let canvas    = null;
let _lgReady  = false;
let _fleetInitialized = false;

// --- Auth token support ---
let _apiToken = localStorage.getItem('oaio-api-token') || '';

async function _fetch(url, opts = {}) {
  if (_apiToken) {
    opts.headers = opts.headers || {};
    if (opts.headers instanceof Headers) {
      opts.headers.set('Authorization', 'Bearer ' + _apiToken);
    } else {
      opts.headers['Authorization'] = 'Bearer ' + _apiToken;
    }
  }
  const r = await fetch(url, opts);
  if (r.status === 401) {
    const token = prompt('API token required:');
    if (token) {
      _apiToken = token.trim();
      localStorage.setItem('oaio-api-token', _apiToken);
      opts.headers = opts.headers || {};
      if (opts.headers instanceof Headers) {
        opts.headers.set('Authorization', 'Bearer ' + _apiToken);
      } else {
        opts.headers['Authorization'] = 'Bearer ' + _apiToken;
      }
      return fetch(url, opts);
    }
  }
  return r;
}

function _wsUrl(path) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let url = proto + '//' + location.host + path;
  if (_apiToken) url += (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(_apiToken);
  return url;
}

function _loadScripts(urls, cb) {
  if (!urls.length) { cb(); return; }
  const s = document.createElement("script");
  s.src    = urls[0];
  s.onload  = () => _loadScripts(urls.slice(1), cb);
  s.onerror = () => { console.warn("Failed to load", urls[0]); _loadScripts(urls.slice(1), cb); };
  document.head.appendChild(s);
}

// Debounced graph save
let _graphSaveTimer = null;
function scheduleGraphSave() {
  if (_graphSaveTimer) clearTimeout(_graphSaveTimer);
  _graphSaveTimer = setTimeout(saveGraph, 500);
}

function saveGraph() {
  if (!graph) return;
  const data = graph.serialize();
  _fetch(`${OLLMO_API}/config/nodes`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ graphData: data }),
  }).catch(() => {});
}

function createDefaultGraph() {
  [
    ["oAIo/ollama",     [60,  60]],
    ["oAIo/kokoro-tts", [60,  220]],
    ["oAIo/rvc",        [320, 220]],
    ["oAIo/open-webui", [320, 60]],
    ["oAIo/f5-tts",     [60,  380]],
    ["oAIo/comfyui",    [580, 60]],
  ].forEach(([type, pos]) => {
    const node = LiteGraph.createNode(type);
    if (node) { node.pos = pos; graph.add(node); }
  });
}

// Routing-driven auto-wire: map routing.json connections to LiteGraph links
// Defines which output port on source connects to which input port on target
const ROUTING_WIRE_MAP = [
  // routing key          source service   source output slot   target service   target input slot
  { key: "ollama_url",    from: "open-webui", fromSlot: "llm_req",     to: "ollama",     toSlot: "llm_req" },
  { key: "tts_url",       from: "open-webui", fromSlot: "tts_req",     to: "rvc",         toSlot: "raw_audio" },
  { key: "image_gen_url", from: "open-webui", fromSlot: "imggen_req",  to: "comfyui",     toSlot: "imggen_req" },
  // Implicit audio chain wires (not in routing.json but architecturally fixed)
  { implicit: true, from: "kokoro-tts",  fromSlot: "raw_audio",   to: "rvc",    toSlot: "raw_audio" },
  { implicit: true, from: "f5-tts",      fromSlot: "clone_audio", to: "rvc",    toSlot: "clone_audio" },
  { implicit: true, from: "ollama",      fromSlot: "llm_resp",    to: "open-webui", toSlot: "llm_resp" },
  { implicit: true, from: "rvc",         fromSlot: "tts_audio",   to: "open-webui", toSlot: "tts_audio" },
  { implicit: true, from: "comfyui",     fromSlot: "image",       to: "open-webui", toSlot: "image" },
];

function _findNode(svcName) {
  return (graph._nodes || []).find(n => n._svc?.name === svcName);
}

function _findSlotIdx(node, slotName, isOutput) {
  const slots = isOutput ? node.outputs : node.inputs;
  if (!slots) return -1;
  return slots.findIndex(s => s.name === slotName);
}

async function autoWireFromRouting() {
  if (!graph) return;
  let routing = {};
  try {
    const r = await _fetch(`${OLLMO_API}/config/routing`);
    routing = await r.json();
  } catch {}

  ROUTING_WIRE_MAP.forEach(wire => {
    // Skip routing-key wires if that route isn't configured
    if (wire.key && !routing[wire.key]) return;

    const fromNode = _findNode(wire.from);
    const toNode   = _findNode(wire.to);
    if (!fromNode || !toNode) return;

    const outIdx = _findSlotIdx(fromNode, wire.fromSlot, true);
    const inIdx  = _findSlotIdx(toNode, wire.toSlot, false);
    if (outIdx < 0 || inIdx < 0) return;

    // Don't create duplicate links
    const existing = Object.values(graph.links || {}).some(l =>
      l && l.origin_id === fromNode.id && l.target_id === toNode.id &&
      l.origin_slot === outIdx && l.target_slot === inIdx
    );
    if (existing) return;

    fromNode.connect(outIdx, toNode, inIdx);
  });

  canvas?.setDirty(true, true);
  scheduleGraphSave();
}

// Sync wires from routing.json after routing panel save
async function syncWiresFromRouting() {
  if (!graph) return;
  // Remove all routing-key-based links, then re-wire
  ROUTING_WIRE_MAP.forEach(wire => {
    if (!wire.key) return; // skip implicit wires
    const fromNode = _findNode(wire.from);
    const toNode   = _findNode(wire.to);
    if (!fromNode || !toNode) return;
    const outIdx = _findSlotIdx(fromNode, wire.fromSlot, true);
    const inIdx  = _findSlotIdx(toNode, wire.toSlot, false);
    if (outIdx < 0 || inIdx < 0) return;
    // Find and remove existing link
    const linkId = Object.keys(graph.links || {}).find(id => {
      const l = graph.links[id];
      return l && l.origin_id === fromNode.id && l.target_id === toNode.id &&
             l.origin_slot === outIdx && l.target_slot === inIdx;
    });
    if (linkId) fromNode.disconnectOutput(outIdx, toNode);
  });
  await autoWireFromRouting();
}

// When user changes wires on canvas, update routing.json to match
let _routingSyncTimer = null;
function syncRoutingFromWires() {
  if (_routingSyncTimer) clearTimeout(_routingSyncTimer);
  _routingSyncTimer = setTimeout(_doSyncRoutingFromWires, 600);
}

async function _doSyncRoutingFromWires() {
  if (!graph) return;
  const updates = {};

  // Check each routing-key wire to see if the connection exists
  ROUTING_WIRE_MAP.forEach(wire => {
    if (!wire.key) return;
    const fromNode = _findNode(wire.from);
    const toNode   = _findNode(wire.to);
    if (!fromNode || !toNode) return;
    const outIdx = _findSlotIdx(fromNode, wire.fromSlot, true);
    const inIdx  = _findSlotIdx(toNode, wire.toSlot, false);
    if (outIdx < 0 || inIdx < 0) return;

    const connected = Object.values(graph.links || {}).some(l =>
      l && l.origin_id === fromNode.id && l.target_id === toNode.id &&
      l.origin_slot === outIdx && l.target_slot === inIdx
    );

    if (!connected) {
      updates[wire.key] = "";
    }
    // If connected, routing stays as-is (we don't know the URL from the wire)
  });

  if (Object.keys(updates).length > 0) {
    try {
      await _fetch(`${OLLMO_API}/config/routing`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(updates),
      });
    } catch {}
  }
}

async function initLiteGraph() {
  if (graph) return;
  if (!_lgReady) {
    const base = ["litegraph.js", "nodes/services.js", "nodes/capabilities.js"];
    const ext  = window._pendingExtNodes || [];
    _loadScripts([...base, ...ext], () => {
      window._pendingExtNodes = [];
      _lgReady = true;
      initLiteGraph();
    });
    return;
  }
  try {
    graph  = new LGraph();
    canvas = new LGraphCanvas("#node-canvas", graph, { autoresize: true });
    canvas.background_image  = null;
    canvas.render_shadows    = false;
    canvas.show_info         = false;
    canvas.allow_dragcanvas  = true;
    canvas.allow_dragnodes   = true;
    canvas.allow_mousewheel  = true;
    canvas.allow_searchbox   = false;
    canvas.ds.scale          = 1.0;

    // Disable LiteGraph's built-in context menu — we use our own
    canvas.processContextMenu = function() {};
    canvas.resize();

    // Try loading saved graph
    let loaded = false;
    try {
      const r = await _fetch(`${OLLMO_API}/config/nodes`);
      const cfg = await r.json();
      if (cfg.graphData && cfg.graphData.nodes && cfg.graphData.nodes.length > 0) {
        graph.configure(cfg.graphData);
        loaded = true;
      }
    } catch {}

    if (!loaded) createDefaultGraph();

    graph.start();

    // Auto-wire from routing.json if no existing links
    const hasLinks = Object.values(graph.links || {}).some(l => l);
    if (!hasLinks || !loaded) {
      await autoWireFromRouting();
    }

    // Auto-save on changes
    const origAdd = graph.add.bind(graph);
    graph.add = function(node) { origAdd(node); scheduleGraphSave(); };
    const origRemove = graph.remove.bind(graph);
    graph.remove = function(node) { origRemove(node); scheduleGraphSave(); };

    // Wire canvas callbacks
    canvas.onNodeDblClick = node => {
      if (node && node._svc) enterContainer(node);
    };

    canvas.onNodeSelected = node => {
      if (!node) return;
      selectedNode = node;
      showConfigView("node");
      const s = node._svc || {};
      document.getElementById("config-node-name").textContent  = node.title || node.type;
      document.getElementById("config-node-group").textContent = s.group || "";
      document.getElementById("config-vram-est").textContent   = s.vramEst ? `${s.vramEst}GB VRAM` : "";
      document.getElementById("config-ram-est").textContent    = s.ramUsed ? `${s.ramUsed}GB RAM` : "";
      document.getElementById("config-port").textContent       = s.port ? `:${s.port}` : "";
      document.getElementById("config-status").textContent     = s.status || "";
      loadNodeConfig(node);
      loadNodeRouting(node);
    };

    canvas.onNodeMoved = () => { scheduleGraphSave(); drawGroupBoxes(); };
    canvas.onConnectionChange = () => { scheduleGraphSave(); renderConnectionMap(); syncRoutingFromWires(); };

    // Redraw group boxes when canvas is panned/zoomed
    const origDraw = canvas.draw.bind(canvas);
    canvas.draw = function() { origDraw(...arguments); drawGroupBoxes(); };

  } catch (e) {
    console.error("LiteGraph init failed:", e);
  }
}

// Sync LiteGraph node statuses from WS service data + push sparkline samples
function syncNodeStatuses(services, vramData) {
  if (!graph) return;
  const byName = {};
  services.forEach(s => { byName[s.name] = s; });
  let dirty = false;
  const _cs = getComputedStyle(document.documentElement);
  graph._nodes.forEach(node => {
    if (!node._svc) return;
    const live = byName[node._svc.name];
    if (!live) return;
    const newStatus = live.status || "unknown";
    const newRam = live.ram_used_gb || 0;
    // Update vramEst from live WS data if available (stays current with services.json edits)
    if (live.vram_est_gb !== undefined) node._svc.vramEst = live.vram_est_gb;
    if (live.ram_est_gb !== undefined) node._svc.ramEst = live.ram_est_gb;
    if (live.memory_mode) node._svc.memoryMode = live.memory_mode;
    if (node._svc.status !== newStatus || node._svc.ramUsed !== newRam) {
      const statusChanged = node._svc.status !== newStatus;
      node._svc.status = newStatus;
      node._svc.ramUsed = newRam;
      dirty = true;
      // Update title bar color on status change
      if (statusChanged) {
        const _green = _cs.getPropertyValue("--tier-ram-bg").trim() || "#0a2a14";
        const _amber = _cs.getPropertyValue("--tier-sata-bg").trim() || "#2a1e00";
        node.color = newStatus === "running" ? _green
                   : newStatus === "stopped" || newStatus === "exited" ? _amber
                   : _cs.getPropertyValue("--tier-nvme-bg").trim() || "#0d1a2f";
      }
    }
    // Push sparkline sample: per-service resource consumption
    if (typeof node._sparkPush === "function") {
      let val;
      if (newStatus === "running") {
        const memMode = node._svc.memoryMode || "vram";
        if (memMode === "vram" && node._svc.vramEst > 0) {
          // VRAM service: show estimated VRAM consumption
          val = node._svc.vramEst;
        } else if (node._svc.ramEst > 0) {
          // RAM-only service: show estimated RAM consumption
          val = node._svc.ramEst;
        } else {
          // Fallback: binary running indicator
          val = 1;
        }
      } else {
        val = 0;
      }
      node._sparkPush(val);
    }
  });
  if (dirty && canvas) canvas.setDirty(true, true);
}

function resizeNodeCanvas() {
  if (!canvas) return;
  // Let layout settle, then resize
  requestAnimationFrame(() => {
    canvas.resize();
    canvas.setDirty(true, true);
  });
}
window.addEventListener("resize", resizeNodeCanvas);

// --- Tab switching ---
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach(t => t.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "live" || btn.dataset.tab === "config") { _liveTabActive = true; startLiveMonitor(); } else { _liveTabActive = false; stopLiveMonitor(); }
    if (btn.dataset.tab === "advanced") loadAdvancedTab();
    if (btn.dataset.tab === "api") loadApiTab(); else unloadApiTab();
    if (btn.dataset.tab === "config") {
      initLiteGraph();
      requestAnimationFrame(() => { resizeNodeCanvas(); drawGroupBoxes(); renderConnectionMap(); });
    }
    if (btn.dataset.tab === "fleet" && !_fleetInitialized) {
      _fleetInitialized = true;
      initFleetTab();
    }
  });
});

// --- Section toggles (TELEMETRY / ACCOUNTING) ---
// Sections start hidden — inline style="display:none" in HTML handles initial state

document.querySelectorAll(".sec-toggle").forEach(btn => {
  btn.addEventListener("click", () => {
    btn.classList.toggle("active");
    const on  = btn.classList.contains("active");
    const sec = btn.dataset.section;
    if (sec === "telemetry") {
      document.getElementById("telemetry-strip").style.display = on ? "" : "none";
    }
    if (sec === "accounting") {
      document.getElementById("acct-strip").style.display = on ? "" : "none";
    }
  });
});

// --- Hamburger menu toggle ---
document.getElementById("nav-hamburger").addEventListener("click", (e) => {
  e.stopPropagation();
  document.querySelector(".tab-nav-left").classList.toggle("open");
});
document.addEventListener("click", (e) => {
  const menu = document.querySelector(".tab-nav-left");
  const btn = document.getElementById("nav-hamburger");
  if (!menu.contains(e.target) && e.target !== btn) {
    menu.classList.remove("open");
  }
});

// --- Gauges ---
function setGauge(barId, labelId, used, total) {
  const pct = Math.min(used / total, 1);
  const bar = document.getElementById(barId);
  if (!bar) return;
  bar.style.width = (pct * 100) + "%";
  bar.className = "fill" + (pct > 0.85 ? " hot" : pct > 0.6 ? " warn" : "");
  const label = document.getElementById(labelId);
  if (!label) return;
  label.textContent = `${used.toFixed(1)} / ${total} GB`;
}

// --- Mode filter pills ---
let activeFilterMode = "all";

function renderModePills() {
  const container = document.getElementById("mode-filter");
  if (!container) return;
  // Remove old dynamic pills (keep ALL)
  container.querySelectorAll(".grp-pill.dyn-mode").forEach(p => p.remove());
  Object.entries(modesData).forEach(([key, mode]) => {
    const pill = document.createElement("button");
    pill.className = "grp-pill dyn-mode" + (activeFilterMode === key ? " active" : "");
    pill.dataset.mode = key;
    pill.textContent = mode.name || key.toUpperCase();
    pill.addEventListener("click", () => {
      activeFilterMode = activeFilterMode === key ? "all" : key;
      container.querySelectorAll(".grp-pill").forEach(p => {
        if (p.dataset.mode === "all") p.classList.toggle("active", activeFilterMode === "all");
        else p.classList.toggle("active", p.dataset.mode === activeFilterMode);
      });
      applyModeFilter();
      drawGroupBoxes();
      renderConnectionMap();
    });
    container.appendChild(pill);
  });
}

// ALL pill click
document.querySelector('#mode-filter .grp-pill[data-mode="all"]')?.addEventListener("click", () => {
  activeFilterMode = "all";
  document.querySelectorAll("#mode-filter .grp-pill").forEach(p =>
    p.classList.toggle("active", p.dataset.mode === "all")
  );
  applyModeFilter();
  drawGroupBoxes();
  renderConnectionMap();
});

function _nodeColors() {
  const cs = getComputedStyle(document.documentElement);
  return {
    bg3:    cs.getPropertyValue("--bg3").trim()    || "#161616",
    bg2:    cs.getPropertyValue("--bg2").trim()    || "#0f0f0f",
    border: cs.getPropertyValue("--border").trim() || "#252525",
    groups: {
      oLLM:    cs.getPropertyValue("--grp-llm").trim()     || "#42a5f5",
      oAudio:  cs.getPropertyValue("--grp-audio").trim()   || "#ffa726",
      Render:  cs.getPropertyValue("--grp-render").trim()   || "#66bb6a",
      Control: cs.getPropertyValue("--grp-control").trim()  || "#78909c",
    },
  };
}
function _groupBg(hex) {
  // Convert group accent color to a subtle dark background (12% opacity approximation)
  const r = parseInt(hex.slice(1,3), 16), g = parseInt(hex.slice(3,5), 16), b = parseInt(hex.slice(5,7), 16);
  return `rgb(${Math.round(r*0.12)}, ${Math.round(g*0.12)}, ${Math.round(b*0.12)})`;
}
function applyModeFilter() {
  if (!graph) return;
  if (!canvas) return;
  const mode = modesData[activeFilterMode];
  const modeServices = mode ? mode.services : null;
  const nc = _nodeColors();
  (graph._nodes || []).forEach(node => {
    const svcName = node._svc?.name;
    const dim = modeServices && svcName && !modeServices.includes(svcName);
    if (dim) {
      node.color   = nc.border;
      node.bgcolor = nc.bg2;
    } else {
      // Use group color for active nodes — don't override status color from _refreshStatus
      const grpColor = nc.groups[node._svc?.group] || nc.bg3;
      node.bgcolor = _groupBg(grpColor);
    }
  });
  canvas.setDirty(true, true);
}

// --- Mode group boxes (canvas overlay) ---
let _lastGroupBoxState = "";
function drawGroupBoxes() {
  if (!graph || !canvas) return;

  const wrap = document.getElementById("canvas-wrap");
  if (!wrap) return;

  // Dirty-check: build a key from positions, scale, offset, and active filter
  const ds = canvas.ds;
  const nodeSnap = (graph._nodes || []).map(n =>
    `${n.id}:${n.pos[0]|0},${n.pos[1]|0},${(n.size?.[0]||200)|0},${(n.size?.[1]||100)|0}`
  ).join(";");
  const stateKey = `${activeFilterMode}|${ds.scale.toFixed(4)}|${ds.offset[0].toFixed(1)},${ds.offset[1].toFixed(1)}|${Object.keys(modesData).join(",")}|${nodeSnap}`;
  if (stateKey === _lastGroupBoxState) return;
  _lastGroupBoxState = stateKey;

  // Remove old boxes
  document.querySelectorAll(".mode-group-box").forEach(el => el.remove());

  const modes = activeFilterMode === "all"
    ? Object.entries(modesData)
    : [[activeFilterMode, modesData[activeFilterMode]]].filter(([, v]) => v);

  const cs = getComputedStyle(document.documentElement);
  const MODE_COLORS = {
    ollmo:          cs.getPropertyValue("--mode-ollmo").trim()   || "#e879f9",
    oaudio:         cs.getPropertyValue("--mode-oaudio").trim()  || "#22d3ee",
    "comfyui-flex": cs.getPropertyValue("--mode-comfyui").trim() || "#facc15",
  };
  const FALLBACK_COLORS = [
    cs.getPropertyValue("--purple").trim() || "#a855f7",
    cs.getPropertyValue("--cyan").trim() || "#00d2be",
    cs.getPropertyValue("--yellow").trim() || "#ffd740",
    cs.getPropertyValue("--grp-control").trim() || "#78909c",
    cs.getPropertyValue("--mode-ollmo").trim() || "#e879f9",
  ];

  modes.forEach(([key, mode], idx) => {
    if (!mode || !mode.services) return;
    // Find nodes belonging to this mode
    const memberNodes = (graph._nodes || []).filter(n =>
      n._svc?.name && mode.services.includes(n._svc.name)
    );
    if (memberNodes.length === 0) return;

    // Compute bounding box in canvas coords
    const PAD = 30;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    memberNodes.forEach(n => {
      minX = Math.min(minX, n.pos[0]);
      minY = Math.min(minY, n.pos[1]);
      maxX = Math.max(maxX, n.pos[0] + (n.size?.[0] || 200));
      maxY = Math.max(maxY, n.pos[1] + (n.size?.[1] || 100));
    });

    // Convert canvas coords to screen coords: screenPos = (canvasPos + offset) * scale
    const sx = (minX - PAD + ds.offset[0]) * ds.scale;
    const sy = (minY - PAD - 14 + ds.offset[1]) * ds.scale;
    const sw = (maxX - minX + PAD * 2) * ds.scale;
    const sh = (maxY - minY + PAD * 2 + 14) * ds.scale;

    const color = MODE_COLORS[key] || FALLBACK_COLORS[idx % FALLBACK_COLORS.length];
    const box = document.createElement("div");
    box.className = "mode-group-box";
    box.style.left = sx + "px";
    box.style.top = sy + "px";
    box.style.width = sw + "px";
    box.style.height = sh + "px";
    box.style.borderColor = color;
    box.style.background = color + "08";

    const label = document.createElement("span");
    label.className = "mode-group-label";
    label.textContent = mode.name || key;
    label.style.color = color;
    label.addEventListener("click", () => loadModeConfig(key));
    box.appendChild(label);

    wrap.appendChild(box);
  });
}

// --- Connection map strip ---
function renderConnectionMap() {
  const el = document.getElementById("connection-map");
  if (!el || !graph) { if (el) el.innerHTML = ""; return; }

  const links = graph.links || {};
  const entries = [];
  Object.values(links).forEach(link => {
    if (!link) return;
    const srcNode = graph.getNodeById(link.origin_id);
    const dstNode = graph.getNodeById(link.target_id);
    if (!srcNode || !dstNode) return;
    const srcName = srcNode._svc?.name || srcNode.title || "?";
    const dstName = dstNode._svc?.name || dstNode.title || "?";
    // Filter by active mode if one is selected
    if (activeFilterMode !== "all") {
      const mode = modesData[activeFilterMode];
      if (mode && mode.services) {
        if (!mode.services.includes(srcNode._svc?.name) && !mode.services.includes(dstNode._svc?.name)) return;
      }
    }
    entries.push(`<span class="conn-entry">${srcName} <span class="conn-arrow">──→</span> ${dstName}</span>`);
  });
  el.innerHTML = entries.length > 0 ? entries.join("") : '<span class="dim">no connections</span>';
}

// --- Mode system ---
let activeModes = [];
let modesData   = {};

async function fetchModesData() {
  try {
    const r = await _fetch(`${OLLMO_API}/modes`);
    modesData = await r.json();
    renderModePills();
    renderModeStrip();
  } catch {}
}

function combinedVram() {
  return activeModes.reduce((s, id) => s + (modesData[id]?.vram_est_gb || 0), 0);
}

// Render active-mode tabs in topbar
function renderModeTabs() {
  const el = document.getElementById("active-tabs");
  if (activeModes.length === 0) {
    el.innerHTML = '<span id="no-mode">NO MODE</span>';
    return;
  }
  el.innerHTML = "";
  activeModes.forEach((modeId, i) => {
    const info  = modesData[modeId];
    const label = info?.name || modeId.toUpperCase();
    const vram  = info?.vram_est_gb ?? "?";
    const tab   = document.createElement("div");
    tab.className = "mode-tab active";
    tab.dataset.mode = modeId;
    tab.innerHTML =
      `<span>${_esc(label)}</span>` +
      `<span class="tab-vram">${_esc(vram)}GB</span>` +
      `<span class="tab-close" data-id="${_esc(modeId)}">×</span>`;
    tab.querySelector(".tab-close").addEventListener("click", e => {
      e.stopPropagation();
      deactivateMode(modeId);
    });
    tab.addEventListener("click", () => loadModeConfig(modeId));
    el.appendChild(tab);
  });
  const total = combinedVram();
  if (activeModes.length === 2 && total > 17) {
    const warn = document.createElement("span");
    warn.className   = "vram-warn";
    warn.textContent = `~${total}GB ⚠`;
    el.appendChild(warn);
  }
}

// Render mode-grid in LIVE tab
function renderModeGrid() {
  const grid = document.getElementById("mode-grid");
  if (!grid) return;
  grid.innerHTML = "";
  const keys = Object.keys(modesData);
  keys.forEach(id => {
    const info = modesData[id];
    const idx  = activeModes.indexOf(id);
    const btn  = document.createElement("button");
    btn.className = "mode-card-btn" + (idx >= 0 ? " active" : "");
    btn.dataset.mode = id;
    btn.innerHTML =
      `<span class="mcb-name">${_esc(info?.name || id.toUpperCase())}</span>` +
      `<span class="mcb-vram">${_esc(info?.vram_budget_gb ?? info?.vram_est_gb ?? "?")}GB</span>`;
    btn.addEventListener("click", () => onModeCardClick(id, info || {}));
    grid.appendChild(btn);
  });
}

// Mode confirm panel
let _pendingModeId = null;

async function onModeCardClick(modeId, info) {
  // Already active → deactivate
  if (activeModes.includes(modeId)) {
    deactivateMode(modeId);
    hideConfirm();
    return;
  }

  _pendingModeId = modeId;

  let projection = null;
  try {
    const r = await _fetch(`${OLLMO_API}/modes/${modeId}/check`);
    projection = await r.json();
  } catch {}

  const blocked   = !!projection?.blocked;
  const projGb    = projection?.projected_gb?.toFixed(1) ?? "?";
  const headroomGb = projection?.headroom_gb?.toFixed(1) ?? "?";

  document.getElementById("mc-title").textContent =
    (info.name || modeId).toUpperCase();

  const statEl = document.getElementById("mc-stat");
  statEl.className = "mode-confirm-stat" + (blocked ? " hot" : "");
  statEl.textContent = projection
    ? `~${projGb}GB → ${headroomGb}GB free`
    : "—";

  document.getElementById("mc-svcs").textContent =
    (info.services || []).join(" · ") || "—";

  const blockedEl = document.getElementById("mc-blocked");
  blockedEl.classList.toggle("hidden", !blocked);
  document.getElementById("mc-activate").disabled = blocked;

  document.getElementById("mode-confirm").classList.remove("hidden");
}

function hideConfirm() {
  document.getElementById("mode-confirm").classList.add("hidden");
  _pendingModeId = null;
}

document.getElementById("mc-cancel").addEventListener("click", hideConfirm);

document.getElementById("emergency-kill-btn").addEventListener("click", async () => {
  const btn = document.getElementById("emergency-kill-btn");
  btn.disabled = true;
  btn.textContent = "⬛ KILLING…";
  try {
    await _fetch(`${OLLMO_API}/emergency/kill`, { method: "POST" });
    activeModes = [];
    renderModeTabs();
    renderModeGrid();
    renderModeStrip();
    showAlert("warning", "Emergency kill executed — all services stopped.");
  } catch {}
  setTimeout(() => {
    btn.disabled = false;
    btn.textContent = "⬛ KILL ALL";
  }, 3000);
});

document.getElementById("mc-activate").addEventListener("click", async () => {
  const modeId = _pendingModeId;
  if (!modeId) return;
  hideConfirm();

  if (activeModes.length >= 2) {
    activeModes[1] = modeId;
  } else {
    activeModes.push(modeId);
  }

  renderModeTabs();
  renderModeGrid();

  try {
    const r    = await _fetch(`${OLLMO_API}/modes/${modeId}/activate`, { method: "POST" });
    const data = await r.json();
    if (data.warning) {
      showAlert("warning",
        `${modeId.toUpperCase()} active — ~${data.projection?.projected_gb}GB projected.`
      );
    }
  } catch {}

  await fetchModesData();
  renderModeGrid();
  poll();
});

function deactivateMode(modeId) {
  activeModes = activeModes.filter(m => m !== modeId);
  renderModeTabs();
  renderModeGrid();
  renderModeStrip();
  if (selectedNode) loadModeAllocations(selectedNode);
  _fetch(`${OLLMO_API}/modes/${modeId}/deactivate`, { method: "POST" }).catch(() => {});
}

// --- Mode strip (CONFIG tab) ---
let _expandedModeId = null;
let _modeStripCreating = false;

function renderModeStrip() {
  const container = document.getElementById("mode-strip-cards");
  if (!container) return;
  container.innerHTML = "";
  Object.entries(modesData).forEach(([key, mode]) => {
    const isActive = activeModes.includes(key);
    const card = document.createElement("button");
    card.className = "ms-card" + (isActive ? " active" : "") + (_expandedModeId === key ? " expanded" : "");
    card.dataset.mode = key;

    // Card body: dot + name + vram — click to activate/deactivate
    const body = document.createElement("span");
    body.className = "ms-card-body";
    body.innerHTML =
      `<span class="ms-dot"></span>` +
      `<span>${_esc(mode.name || key)}</span>` +
      `<span class="ms-vram">${_esc(mode.vram_budget_gb ?? mode.vram_est_gb ?? "?")}GB</span>`;
    body.addEventListener("click", (e) => {
      e.stopPropagation();
      _modeStripCreating = false;
      if (isActive) {
        deactivateMode(key);
        if (_expandedModeId === key) collapseModeEditor();
      } else {
        // Activate — same logic as LIVE tab
        if (activeModes.length >= 2) activeModes[1] = key;
        else activeModes.push(key);
        renderModeTabs();
        renderModeGrid();
        renderModeStrip();
        _fetch(`${OLLMO_API}/modes/${key}/activate`, { method: "POST" })
          .then(r => r.json())
          .then(data => {
            if (data.warning) showAlert("warning", `${(mode.name || key).toUpperCase()} active — ~${data.projection?.projected_gb}GB projected.`);
          }).catch(() => {});
        fetchModesData().then(() => { renderModeGrid(); });
        poll();
      }
    });
    card.appendChild(body);

    // Edit button — small arrow to expand/collapse editor
    const editBtn = document.createElement("span");
    editBtn.className = "ms-edit";
    editBtn.textContent = _expandedModeId === key ? "\u25B2" : "\u25BC";
    editBtn.title = "Edit mode";
    editBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      _modeStripCreating = false;
      if (_expandedModeId === key) collapseModeEditor();
      else expandModeEditor(key);
    });
    card.appendChild(editBtn);

    container.appendChild(card);
  });
}

async function expandModeEditor(modeId) {
  _expandedModeId = modeId;
  _modeStripCreating = false;
  renderModeStrip();

  const info = modesData[modeId] || {};
  const cfg  = modeConfigs[modeId] || {};
  const isActive = activeModes.includes(modeId);

  // Fetch all services for checkbox grid
  let allServices = {};
  try {
    const r = await _fetch(`${OLLMO_API}/config/services`);
    allServices = await r.json();
  } catch {}

  const editor = document.getElementById("mode-strip-editor");
  const vramVal = info.vram_budget_gb ?? info.vram_est_gb ?? 10;
  const priority = cfg.priority || "primary";
  const limit = cfg.limit || "soft";

  const svcPills = Object.keys(allServices).map(name => {
    const checked = (info.services || []).includes(name);
    return `<button class="mse-svc-pill${checked ? " checked" : ""}" data-svc="${_esc(name)}">${checked ? "\u2713 " : ""}${_esc(name)}</button>`;
  }).join("");

  editor.innerHTML =
    `<div class="mse-row">` +
      `<input class="mse-name" id="mse-name" value="${_esc(info.name || modeId)}" title="Click to edit name" />` +
      `<input class="mse-desc" id="mse-desc" value="${_esc(info.description || "")}" placeholder="description" />` +
      `<div class="mse-vram-group">` +
        `<span class="mse-label">VRAM</span>` +
        `<input type="range" id="mse-vram" min="0" max="20" step="0.5" value="${vramVal}" />` +
        `<span class="mse-vram-val" id="mse-vram-val">${vramVal} GB</span>` +
      `</div>` +
    `</div>` +
    `<div class="mse-row">` +
      `<span class="mse-label">Services</span>` +
      `<div class="mse-services" id="mse-services">${svcPills}</div>` +
    `</div>` +
    `<div class="mse-row">` +
      `<span class="mse-label">Priority</span>` +
      `<select class="mse-select" id="mse-priority">` +
        `<option value="primary"${priority === "primary" ? " selected" : ""}>primary</option>` +
        `<option value="secondary"${priority === "secondary" ? " selected" : ""}>secondary</option>` +
      `</select>` +
      `<span class="mse-label">Limit</span>` +
      `<select class="mse-select" id="mse-limit">` +
        `<option value="soft"${limit === "soft" ? " selected" : ""}>soft</option>` +
        `<option value="hard"${limit === "hard" ? " selected" : ""}>hard</option>` +
      `</select>` +
      `<div class="mse-actions">` +
        `<button class="mode-btn ${isActive ? "mse-btn-deactivate" : "mse-btn-activate"}" id="mse-toggle">${isActive ? "DEACTIVATE" : "ACTIVATE"}</button>` +
        `<button class="mode-btn mse-btn-delete" id="mse-delete">DELETE</button>` +
        `<button class="mse-btn-close" id="mse-close">\u00d7</button>` +
      `</div>` +
    `</div>`;

  editor.classList.remove("hidden");

  // Wire up event handlers
  document.getElementById("mse-name").addEventListener("blur", async () => {
    const newName = document.getElementById("mse-name").value.trim();
    if (newName && newName !== (info.name || modeId)) {
      await _fetch(`${OLLMO_API}/modes/${encodeURIComponent(modeId)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName }),
      });
      await fetchModesData();
      renderModeStrip();
    }
  });

  document.getElementById("mse-desc").addEventListener("blur", async () => {
    const newDesc = document.getElementById("mse-desc").value.trim();
    if (newDesc !== (info.description || "")) {
      await _fetch(`${OLLMO_API}/modes/${encodeURIComponent(modeId)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ description: newDesc }),
      });
      await fetchModesData();
    }
  });

  const vramSlider = document.getElementById("mse-vram");
  vramSlider.addEventListener("input", () => {
    document.getElementById("mse-vram-val").textContent = vramSlider.value + " GB";
  });
  vramSlider.addEventListener("change", async () => {
    await _fetch(`${OLLMO_API}/modes/${encodeURIComponent(modeId)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vram_budget_gb: parseFloat(vramSlider.value) }),
    });
    await fetchModesData();
    renderModeStrip();
    drawGroupBoxes();
  });

  // Service pills
  document.querySelectorAll("#mse-services .mse-svc-pill").forEach(pill => {
    pill.addEventListener("click", async () => {
      pill.classList.toggle("checked");
      const services = Array.from(document.querySelectorAll("#mse-services .mse-svc-pill.checked"))
        .map(p => p.dataset.svc);
      await _fetch(`${OLLMO_API}/modes/${encodeURIComponent(modeId)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ services }),
      });
      // Update pill text
      pill.textContent = pill.classList.contains("checked") ? "\u2713 " + pill.dataset.svc : pill.dataset.svc;
      await fetchModesData();
      renderModeStrip();
      drawGroupBoxes();
      renderConnectionMap();
    });
  });

  // Priority/limit save to modeConfigs
  document.getElementById("mse-priority").addEventListener("change", () => {
    modeConfigs[modeId] = modeConfigs[modeId] || {};
    modeConfigs[modeId].priority = document.getElementById("mse-priority").value;
    _fetch(`${OLLMO_API}/config/nodes`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ modeConfigs }),
    }).catch(() => {});
  });
  document.getElementById("mse-limit").addEventListener("change", () => {
    modeConfigs[modeId] = modeConfigs[modeId] || {};
    modeConfigs[modeId].limit = document.getElementById("mse-limit").value;
    _fetch(`${OLLMO_API}/config/nodes`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ modeConfigs }),
    }).catch(() => {});
  });

  // Activate/Deactivate
  document.getElementById("mse-toggle").addEventListener("click", async () => {
    if (isActive) {
      deactivateMode(modeId);
      expandModeEditor(modeId); // re-render editor with updated state
    } else {
      if (activeModes.length >= 2) activeModes[1] = modeId;
      else activeModes.push(modeId);
      renderModeTabs();
      renderModeGrid();
      try {
        const r = await _fetch(`${OLLMO_API}/modes/${modeId}/activate`, { method: "POST" });
        const data = await r.json();
        if (data.warning) {
          showAlert("warning", `${(info.name || modeId).toUpperCase()} active — ~${data.projection?.projected_gb}GB projected.`);
        }
      } catch {}
      await fetchModesData();
      renderModeGrid();
      expandModeEditor(modeId);
      poll();
    }
  });

  // Delete
  document.getElementById("mse-delete").addEventListener("click", async () => {
    if (!confirm(`Delete mode "${info.name || modeId}"?`)) return;
    await _fetch(`${OLLMO_API}/modes/${encodeURIComponent(modeId)}`, { method: "DELETE" });
    collapseModeEditor();
    await fetchModesData();
    renderModeGrid();
    renderModePills();
    drawGroupBoxes();
  });

  // Close
  document.getElementById("mse-close").addEventListener("click", () => collapseModeEditor());
}

function collapseModeEditor() {
  _expandedModeId = null;
  _modeStripCreating = false;
  const editor = document.getElementById("mode-strip-editor");
  if (editor) { editor.classList.add("hidden"); editor.innerHTML = ""; }
  renderModeStrip();
}

// + MODE inline create
document.getElementById("mode-strip-add")?.addEventListener("click", async () => {
  if (_modeStripCreating) { collapseModeEditor(); return; }
  _expandedModeId = null;
  _modeStripCreating = true;
  renderModeStrip();

  let allServices = {};
  try {
    const r = await _fetch(`${OLLMO_API}/config/services`);
    allServices = await r.json();
  } catch {}

  const svcPills = Object.keys(allServices).map(name =>
    `<button class="mse-svc-pill" data-svc="${_esc(name)}">${_esc(name)}</button>`
  ).join("");

  const editor = document.getElementById("mode-strip-editor");
  editor.innerHTML =
    `<div class="mse-row">` +
      `<input class="mse-name" id="mse-new-name" placeholder="mode name" />` +
      `<input class="mse-desc" id="mse-new-desc" placeholder="description" />` +
      `<div class="mse-vram-group">` +
        `<span class="mse-label">VRAM</span>` +
        `<input type="range" id="mse-new-vram" min="0" max="20" step="0.5" value="10" />` +
        `<span class="mse-vram-val" id="mse-new-vram-val">10 GB</span>` +
      `</div>` +
    `</div>` +
    `<div class="mse-row">` +
      `<span class="mse-label">Services</span>` +
      `<div class="mse-services" id="mse-new-services">${svcPills}</div>` +
    `</div>` +
    `<div class="mse-row">` +
      `<div class="mse-actions">` +
        `<button class="mode-btn mse-btn-activate" id="mse-new-submit">CREATE</button>` +
        `<button class="mse-btn-close" id="mse-new-cancel">\u00d7</button>` +
      `</div>` +
    `</div>`;
  editor.classList.remove("hidden");

  document.getElementById("mse-new-vram").addEventListener("input", e => {
    document.getElementById("mse-new-vram-val").textContent = e.target.value + " GB";
  });

  document.querySelectorAll("#mse-new-services .mse-svc-pill").forEach(pill => {
    pill.addEventListener("click", () => {
      pill.classList.toggle("checked");
      pill.textContent = pill.classList.contains("checked") ? "\u2713 " + pill.dataset.svc : pill.dataset.svc;
    });
  });

  document.getElementById("mse-new-submit").addEventListener("click", async () => {
    const name = document.getElementById("mse-new-name").value.trim();
    if (!name) return;
    const desc = document.getElementById("mse-new-desc").value.trim();
    const budget = parseFloat(document.getElementById("mse-new-vram").value) || 10;
    const services = Array.from(document.querySelectorAll("#mse-new-services .mse-svc-pill.checked"))
      .map(p => p.dataset.svc);
    try {
      const r = await _fetch(`${OLLMO_API}/modes`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, description: desc, vram_budget_gb: budget, services }),
      });
      const data = await r.json();
      if (data.error) { alert(data.error); return; }
      collapseModeEditor();
      await fetchModesData();
      renderModeGrid();
      renderModePills();
      drawGroupBoxes();
    } catch (e) {
      alert("Failed to create mode: " + e.message);
    }
  });

  document.getElementById("mse-new-cancel").addEventListener("click", () => collapseModeEditor());
});

// SAVE TPL shortcut
document.getElementById("mode-strip-save-tpl")?.addEventListener("click", async () => {
  const name = prompt("Template name:");
  if (!name) return;
  await _fetch(`${OLLMO_API}/templates/save?name=${encodeURIComponent(name)}`, { method: "POST" });
  showAlert("info", `Template "${name}" saved.`);
});

// --- Sub-graph navigation ---
const subGraphCache = {};
const navStack      = [];

let _currentSubSvc = null;

async function enterContainer(node) {
  if (!canvas) return;
  const svcName = node._svc?.name;
  if (!svcName) return;
  if (!subGraphCache[svcName]) {
    subGraphCache[svcName] = await CapabilityNodes.buildSubGraph(svcName);
  }
  navStack.push({ graph: canvas.graph, title: "ALL SERVICES" });
  canvas.graph.stop();
  canvas.graph = subGraphCache[svcName];
  canvas.graph.start();
  canvas.draw(true, true);
  _currentSubSvc = svcName;
  document.getElementById("mode-filter").classList.add("hidden");
  document.getElementById("breadcrumb").classList.remove("hidden");
  document.getElementById("crumb-path").textContent = node.title || svcName.toUpperCase();
  // Show pull model UI when inside Ollama
  document.getElementById("pull-model-wrap").classList.toggle("hidden", svcName !== "ollama");
}

function exitContainer() {
  if (!canvas || navStack.length === 0) return;
  const prev = navStack.pop();
  canvas.graph.stop();
  canvas.graph = prev.graph;
  canvas.graph.start();
  canvas.draw(true, true);
  _currentSubSvc = null;
  document.getElementById("pull-model-wrap").classList.add("hidden");
  if (navStack.length === 0) {
    document.getElementById("mode-filter").classList.remove("hidden");
    document.getElementById("breadcrumb").classList.add("hidden");
  } else {
    document.getElementById("crumb-path").textContent = navStack[navStack.length - 1].title;
  }
}

document.getElementById("crumb-back").addEventListener("click", exitContainer);

// --- Pull Ollama model ---
document.getElementById("pull-model-btn").addEventListener("click", async () => {
  const input  = document.getElementById("pull-model-input");
  const status = document.getElementById("pull-model-status");
  const name   = input.value.trim();
  if (!name) return;
  status.textContent = "pulling...";
  try {
    const r = await _fetch(`${OLLMO_API}/services/ollama/models/pull`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const data = await r.json();
    if (data.error) {
      status.textContent = data.error;
    } else {
      status.textContent = "done";
      input.value = "";
      // Refresh sub-graph
      delete subGraphCache["ollama"];
      if (_currentSubSvc === "ollama") {
        subGraphCache["ollama"] = await CapabilityNodes.buildSubGraph("ollama");
        canvas.graph.stop();
        canvas.graph = subGraphCache["ollama"];
        canvas.graph.start();
        canvas.draw(true, true);
      }
      setTimeout(() => { status.textContent = ""; }, 3000);
    }
  } catch (e) {
    status.textContent = "error";
  }
});

document.getElementById("pull-model-input").addEventListener("keydown", e => {
  if (e.key === "Enter") document.getElementById("pull-model-btn").click();
});


// --- Context menu ---
document.getElementById("node-canvas").addEventListener("contextmenu", e => {
  e.preventDefault();
  e.stopPropagation();
  if (!canvas) return;

  // Remove existing context menu
  const old = document.getElementById("ctx-menu");
  if (old) old.remove();

  const menu = document.createElement("div");
  menu.id = "ctx-menu";
  menu.className = "ctx-menu";
  menu.style.left = e.clientX + "px";
  menu.style.top  = e.clientY + "px";

  const items = [
    ["+ Add Service", () => document.getElementById("add-svc-modal").classList.remove("hidden")],
    ["Refresh Models", async () => {
      Object.keys(subGraphCache).forEach(k => delete subGraphCache[k]);
      if (_currentSubSvc) {
        subGraphCache[_currentSubSvc] = await CapabilityNodes.buildSubGraph(_currentSubSvc);
        canvas.graph.stop();
        canvas.graph = subGraphCache[_currentSubSvc];
        canvas.graph.start();
        canvas.draw(true, true);
      }
    }],
    ["Save Layout", () => saveGraph()],
    ["Reset Layout", () => {
      if (!confirm("Reset to default layout? Current positions will be lost.")) return;
      graph.clear();
      createDefaultGraph();
      scheduleGraphSave();
    }],
    ["Delete Mode…", async () => {
      const keys = Object.keys(modesData);
      if (keys.length === 0) { alert("No modes to delete."); return; }
      const name = prompt("Mode to delete:\n" + keys.join(", "));
      if (!name || !modesData[name]) return;
      if (!confirm(`Delete mode "${modesData[name].name || name}"?`)) return;
      await _fetch(`${OLLMO_API}/modes/${encodeURIComponent(name)}`, { method: "DELETE" });
      await fetchModesData();
      renderModeGrid();
      renderModePills();
      drawGroupBoxes();
    }],
  ];

  items.forEach(([label, action]) => {
    const item = document.createElement("div");
    item.className = "ctx-item";
    item.textContent = label;
    item.addEventListener("click", () => { menu.remove(); action(); });
    menu.appendChild(item);
  });

  document.body.appendChild(menu);
  const close = () => { menu.remove(); document.removeEventListener("click", close); };
  setTimeout(() => document.addEventListener("click", close), 0);
});

// --- Add Service modal ---
document.getElementById("add-svc-btn").addEventListener("click", () => {
  document.getElementById("add-svc-modal").classList.remove("hidden");
});

document.getElementById("svc-cancel").addEventListener("click", () => {
  document.getElementById("add-svc-modal").classList.add("hidden");
});

document.getElementById("svc-submit").addEventListener("click", async () => {
  const name  = document.getElementById("svc-name").value.trim();
  const ctr   = document.getElementById("svc-container").value.trim();
  const port  = parseInt(document.getElementById("svc-port").value) || 0;
  const group = document.getElementById("svc-group").value;
  const vram  = parseFloat(document.getElementById("svc-vram").value) || 0;
  const desc  = document.getElementById("svc-desc").value.trim();

  if (!name) return;

  try {
    const r = await _fetch(`${OLLMO_API}/config/services`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name, container: ctr || name, port,
        group, vram_est_gb: vram, description: desc,
      }),
    });
    const data = await r.json();
    if (data.error) { alert(data.error); return; }

    // Register new node type and add to graph
    const io = (typeof SERVICE_PORTS !== 'undefined' ? SERVICE_PORTS[name] : null) || { in: [["input", "data"]], out: [["output", "data"]] };
    const label = desc ? `${name.charAt(0).toUpperCase() + name.slice(1)} — ${desc}` : name;
    function NodeClass() {
      io.in.forEach(([n, t]) => this.addInput(n, t));
      io.out.forEach(([n, t]) => this.addOutput(n, t));
      this.title   = label;
      const nc = _nodeColors();
      this.color   = nc.color;
      this.bgcolor = nc.bgcolor;
      this.size    = [220, 130];
      this._svc    = { name, group, port, vramEst: vram, ramEst: 0, memoryMode: "vram", status: "unknown", ramUsed: 0 };
      // Sparkline rolling buffer
      this._sparkData  = [];
      this._sparkMax   = 30;
      const _grpKey = {"oLLM":"--grp-llm","oAudio":"--grp-audio","Render":"--grp-render","Control":"--grp-control"}[group];
      this._grpHex = _grpKey ? (getComputedStyle(document.documentElement).getPropertyValue(_grpKey).trim() || "#555") : "#555";
      this._sparkPhase = 0;
      this._sparkAnim  = null;
      this._refreshStatus();
    }
    NodeClass.prototype = Object.create(LGraphNode.prototype);
    NodeClass.prototype._refreshStatus = async function() {
      try {
        const r = await _fetch(`${OLLMO_API}/services/${this._svc.name}/status`);
        const d = await r.json();
        this._svc.status  = d.status || "unknown";
        this._svc.ramUsed = d.ram_used_gb || 0;
      } catch { this._svc.status = "error"; }
      this.setDirtyCanvas(true);
    };
    NodeClass.prototype._sparkPush = function(value) {
      this._sparkData.push(value);
      if (this._sparkData.length > this._sparkMax) this._sparkData.shift();
      if (!this._sparkAnim) this._sparkStartAnim();
    };
    NodeClass.prototype._sparkStartAnim = function() {
      const self = this;
      let last = performance.now();
      const interval = 50;
      function tick(now) {
        self._sparkAnim = requestAnimationFrame(tick);
        if (now - last < interval) return;
        const configTab = document.getElementById("tab-config");
        if (configTab && !configTab.classList.contains("active")) return;
        const dt = (now - last) / 1000;
        last = now;
        self._sparkPhase = (self._sparkPhase || 0) + dt * 3.0;
        if (self._sparkPhase > Math.PI * 200) self._sparkPhase -= Math.PI * 200;
        self.setDirtyCanvas(true);
      }
      this._sparkAnim = requestAnimationFrame(tick);
    };
    NodeClass.prototype.onRemoved = function() {
      if (this._sparkAnim) { cancelAnimationFrame(this._sparkAnim); this._sparkAnim = null; }
    };
    NodeClass.prototype.onDrawBackground = function(ctx) {
      const s = this._svc;
      const cs = getComputedStyle(document.documentElement);
      const running = s.status === "running";
      const cGreen = cs.getPropertyValue("--green").trim() || "#00e676";
      const cYellow = cs.getPropertyValue("--yellow").trim() || "#ffd740";
      const cDim = cs.getPropertyValue("--text-dim").trim() || "#555";
      const color = running ? cGreen : s.status === "stopped" || s.status === "exited" ? cDim : cYellow;
      // Status dot
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(this.size[0] - 14, 14, 5, 0, Math.PI * 2);
      ctx.fill();
      // Toggle button (power icon area)
      const bx = this.size[0] - 32, by = 4, bw = 26, bh = 20;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.strokeRect(bx, by, bw, bh);
      ctx.fillStyle = color;
      ctx.font = "bold 10px monospace";
      ctx.textAlign = "center";
      ctx.fillText(running ? "ON" : "OFF", bx + bw / 2, by + 14);
      ctx.textAlign = "left";
    };
    NodeClass.prototype.onDrawForeground = function(ctx) {
      const s = this._svc;
      const _cs = getComputedStyle(document.documentElement);
      ctx.fillStyle = _cs.getPropertyValue("--text-dim").trim() || "#555";
      ctx.font = "10px monospace";
      const memMode = s.memoryMode || "vram";
      const resVal = memMode === "vram" && s.vramEst > 0 ? s.vramEst : (s.ramEst || 0);
      const resType = memMode === "vram" && s.vramEst > 0 ? "VRAM" : "RAM";
      ctx.fillText(`${resType}: ${resVal}GB est`, 8, this.size[1] - 62);
      ctx.fillText(`RAM used: ${s.ramUsed}GB`, 8, this.size[1] - 48);
      ctx.fillText(`Group: ${s.group}`, 8, this.size[1] - 34);

      // ── Sparkline ──────────────────────────────────────
      const buf = this._sparkData;
      if (!buf || buf.length < 2) return;
      const pad   = 6;
      const sparkH = 22;
      const sparkY = this.size[1] - sparkH - pad;
      const sparkW = this.size[0] - pad * 2;
      const sparkX = pad;
      let maxVal = 0;
      for (let i = 0; i < buf.length; i++) { if (buf[i] > maxVal) maxVal = buf[i]; }
      if (maxVal < 0.01) maxVal = 1;
      const stepX = sparkW / (this._sparkMax - 1);
      const pts = [];
      for (let i = 0; i < buf.length; i++) {
        pts.push({
          x: sparkX + (this._sparkMax - buf.length + i) * stepX,
          y: sparkY + sparkH - (buf[i] / maxVal) * sparkH,
        });
      }
      const grpColor = this._grpHex;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(pts[0].x, sparkY + sparkH);
      for (let i = 0; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
      ctx.lineTo(pts[pts.length - 1].x, sparkY + sparkH);
      ctx.closePath();
      ctx.fillStyle = grpColor + "18";
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
      ctx.strokeStyle = grpColor + "cc";
      ctx.lineWidth   = 1.5;
      ctx.lineJoin    = "round";
      ctx.stroke();
      if (s.status === "running") {
        const last = pts[pts.length - 1];
        const noAnim = document.body.classList.contains("no-animations");
        const phase = noAnim ? 0 : (this._sparkPhase || 0);
        const glowR = noAnim ? 2.5 : 2.5 + Math.sin(phase) * 1.0;
        const glowA = noAnim ? 0.7 : 0.6 + Math.sin(phase) * 0.25;
        ctx.beginPath();
        ctx.arc(last.x, last.y, glowR, 0, Math.PI * 2);
        ctx.fillStyle = grpColor;
        ctx.globalAlpha = glowA;
        ctx.fill();
        if (!noAnim) {
          ctx.beginPath();
          ctx.arc(last.x, last.y, glowR + 2.5, 0, Math.PI * 2);
          ctx.fillStyle = grpColor;
          ctx.globalAlpha = glowA * 0.25;
          ctx.fill();
        }
      }
      ctx.restore();
    };
    NodeClass.prototype.onMouseDown = function(e, pos) {
      if (pos[0] > this.size[0] - 34 && pos[1] < 26) {
        const action = this._svc.status === "running" ? "stop" : "start";
        _fetch(`${OLLMO_API}/services/${this._svc.name}/${action}`, { method: "POST" })
          .then(() => this._refreshStatus());
        return true;
      }
    };
    NodeClass.title = label;
    LiteGraph.registerNodeType(`oAIo/${name}`, NodeClass);

    if (graph) {
      const node = LiteGraph.createNode(`oAIo/${name}`);
      if (node) {
        node.pos = [100, 100];
        graph.add(node);
        scheduleGraphSave();
      }
    }

    document.getElementById("add-svc-modal").classList.add("hidden");
    // Clear form
    document.getElementById("svc-name").value = "";
    document.getElementById("svc-container").value = "";
    document.getElementById("svc-port").value = "0";
    document.getElementById("svc-vram").value = "0";
    document.getElementById("svc-desc").value = "";
  } catch (e) {
    alert("Failed to add service: " + e.message);
  }
});

// --- Create Mode — redirect canvas-nav button to mode strip inline create ---
document.getElementById("add-mode-btn").addEventListener("click", () => {
  document.getElementById("mode-strip-add")?.click();
});

document.getElementById("mode-new-cancel").addEventListener("click", () => {
  document.getElementById("add-mode-modal").classList.add("hidden");
});

document.getElementById("mode-new-submit").addEventListener("click", async () => {
  const name   = document.getElementById("mode-new-name").value.trim();
  const desc   = document.getElementById("mode-new-desc").value.trim();
  const budget = parseFloat(document.getElementById("mode-new-budget").value) || 10;
  const checks = document.querySelectorAll("#mode-new-services input[type=checkbox]:checked");
  const services = Array.from(checks).map(cb => cb.value);

  if (!name) return;

  try {
    const r = await _fetch(`${OLLMO_API}/modes`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description: desc, vram_budget_gb: budget, services }),
    });
    const data = await r.json();
    if (data.error) { alert(data.error); return; }

    // Refresh modes everywhere
    await fetchModesData();
    renderModeGrid();
    renderModePills();
    drawGroupBoxes();

    document.getElementById("add-mode-modal").classList.add("hidden");
    document.getElementById("mode-new-name").value = "";
    document.getElementById("mode-new-desc").value = "";
    document.getElementById("mode-new-budget").value = "10";
  } catch (e) {
    alert("Failed to create mode: " + e.message);
  }
});

// --- Config panel scope switching ---
function showConfigView(view) {
  document.getElementById("config-node").classList.toggle("hidden",  view !== "node");
  const modeEl = document.getElementById("config-mode");
  if (modeEl) modeEl.classList.toggle("hidden", view !== "mode");
  document.getElementById("config-paths").classList.toggle("hidden", view !== "paths");
}

// --- Load mode config into the mode panel ---
function loadModeConfig(modeId) {
  showConfigView("mode");
  const mode = modesData[modeId] || {};
  document.getElementById("config-node-name").textContent  = mode.name || modeId;
  document.getElementById("config-node-group").textContent = "mode";
  document.getElementById("config-vram-est").textContent   = mode.vram_est_gb ? `${mode.vram_est_gb}GB VRAM` : "";
  document.getElementById("config-ram-est").textContent    = "";
  const body = document.getElementById("mode-config-body");
  if (body) {
    const services = (mode.services || []).join(", ") || "none";
    body.innerHTML =
      `<div class="config-col"><div class="col-label">MODE: ${_esc(mode.name || modeId)}</div>` +
      `<div class="config-field"><div class="config-row"><label>Services</label><span>${_esc(services)}</span></div></div>` +
      (mode.description ? `<div class="config-field"><div class="config-row"><label>Description</label><span>${_esc(mode.description)}</span></div></div>` : "") +
      `</div>`;
  }
}

// --- Load persisted node configs ---
async function initConfigs() {
  try {
    const r = await _fetch(`${OLLMO_API}/config/nodes`);
    const data = await r.json();
    if (data.nodeConfigs) Object.assign(nodeConfigs, data.nodeConfigs);
    if (data.modeConfigs) Object.assign(modeConfigs, data.modeConfigs);
  } catch {}
}
initConfigs();

// --- Docker Discovery Scanner ---
document.getElementById("scan-docker-btn").addEventListener("click", async () => {
  const dropdown = document.getElementById("discovery-dropdown");
  if (!dropdown.classList.contains("hidden")) {
    dropdown.classList.add("hidden");
    return;
  }
  dropdown.innerHTML = '<span class="dim">Scanning...</span>';
  dropdown.classList.remove("hidden");

  try {
    const r = await _fetch(`${OLLMO_API}/docker/discover`);
    const containers = await r.json();
    if (!containers.length) {
      dropdown.innerHTML = '<span class="dim">No unregistered containers found on oaio-net</span>';
      setTimeout(() => dropdown.classList.add("hidden"), 3000);
      return;
    }
    dropdown.innerHTML = "";
    containers.forEach(c => {
      const card = document.createElement("div");
      card.className = "discovery-card";
      const statusClass = c.status === "running" ? "running" : "stopped";
      card.innerHTML =
        `<span class="disc-name">${_esc(c.container)}</span>` +
        `<span class="disc-status ${statusClass}">${_esc(c.status)}</span>` +
        `<span class="disc-ports">${c.ports.length ? _esc(c.ports.join(", ")) : "—"}</span>` +
        `<button class="grp-pill disc-add-btn">ADD</button>`;
      card.querySelector(".disc-add-btn").addEventListener("click", () => {
        document.getElementById("svc-name").value = c.container;
        document.getElementById("svc-container").value = c.container;
        document.getElementById("svc-port").value = c.ports[0] || 0;
        document.getElementById("svc-group").value = "Other";
        document.getElementById("svc-vram").value = "0";
        document.getElementById("svc-desc").value = "";
        document.getElementById("add-svc-modal").classList.remove("hidden");
        dropdown.classList.add("hidden");
      });
      dropdown.appendChild(card);
    });
  } catch (e) {
    dropdown.innerHTML = '<span class="dim">Scan failed: ' + _esc(e.message) + '</span>';
    setTimeout(() => dropdown.classList.add("hidden"), 3000);
  }
});

document.addEventListener("click", (e) => {
  const dropdown = document.getElementById("discovery-dropdown");
  if (!dropdown.contains(e.target) && e.target.id !== "scan-docker-btn") {
    dropdown.classList.add("hidden");
  }
});

// --- Per-node config ---
let selectedNode = null;
let nodeConfigs  = {};

["cfg-memory", "cfg-priority", "cfg-bus", "cfg-limit", "cfg-boot"].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener("change", () => { saveNodeConfig(); updateNodeSubs(); });
});

function saveNodeConfig() {
  if (!selectedNode) return;
  const key = selectedNode.title || selectedNode.type;
  const sub = readSubSettings();
  const cfg = {
    memory:   document.getElementById("cfg-memory").value,
    priority: document.getElementById("cfg-priority").value,
    bus:      document.getElementById("cfg-bus").value,
    limit:    document.getElementById("cfg-limit").value,
    boot:     document.getElementById("cfg-boot").checked,
    sub,
  };
  nodeConfigs[key] = cfg;
  _fetch(`${OLLMO_API}/config/nodes`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nodeConfigs }),
  }).catch(() => {});

  // Also PATCH the actual service in services.json
  const svcName = selectedNode._svc?.name;
  if (svcName) {
    const patch = {
      memory_mode:     cfg.memory,
      priority:        parseInt(cfg.priority) || 1,
      bus_preference:  cfg.bus,
      limit_mode:      cfg.limit,
      boot_with_system: cfg.boot,
    };
    _fetch(`${OLLMO_API}/config/services/${encodeURIComponent(svcName)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }).catch(() => {});
  }
}

function readSubSettings() {
  return {
    vramAlloc: document.getElementById("sub-vram-alloc")?.value,
    ramLimit:  document.getElementById("sub-ram-limit")?.value,
    swapPath:  document.getElementById("sub-swap-path")?.value,
    limitAct:  document.getElementById("sub-limit-action")?.value,
    bootDelay: document.getElementById("sub-boot-delay")?.value,
  };
}

async function loadNodeConfig(node) {
  const key = node.title || node.type;
  const localCfg = nodeConfigs[key] || {};

  // Try loading live values from services.json via API
  const svcName = node._svc?.name;
  let svcCfg = null;
  if (svcName) {
    try {
      const r = await _fetch(`${OLLMO_API}/config/services`);
      const all = await r.json();
      svcCfg = all[svcName];
    } catch {}
  }

  // Service config takes precedence over local nodeConfigs
  const memory   = svcCfg?.memory_mode     || localCfg.memory   || "vram";
  const priority = svcCfg?.priority        || localCfg.priority || 1;
  const bus      = svcCfg?.bus_preference  || localCfg.bus      || "nvme";
  const limit    = svcCfg?.limit_mode      || localCfg.limit    || "soft";
  const boot     = svcCfg?.boot_with_system ?? localCfg.boot    ?? false;

  document.getElementById("cfg-memory").value   = memory;
  document.getElementById("cfg-priority").value = priority;
  document.getElementById("cfg-bus").value      = bus;
  document.getElementById("cfg-limit").value    = limit;
  document.getElementById("cfg-boot").checked   = boot;
  updateNodeSubs(localCfg.sub);
  loadModeAllocations(node);
}

async function loadModeAllocations(node) {
  const svcName = node._svc?.name;
  const body = document.getElementById("mode-allocs-body");
  if (!svcName || activeModes.length === 0) {
    body.innerHTML = '<div class="dim" style="font-size:9px">No active modes</div>';
    return;
  }
  body.innerHTML = "";
  for (const modeId of activeModes) {
    const modeInfo = modesData[modeId] || {};
    let allocs = {}, budget = modeInfo.vram_est_gb || 20;
    try {
      const r = await _fetch(`${OLLMO_API}/modes/${modeId}/allocations`);
      const d = await r.json();
      allocs = d.allocations || {};
      budget = d.vram_budget_gb || budget;
    } catch {}
    const current = allocs[svcName] ?? 0;
    const row = document.createElement("div");
    row.className = "config-field";
    row.innerHTML =
      `<div class="config-row">
         <label>${_esc((modeInfo.name || modeId).toUpperCase())}</label>
         <input type="range" class="alloc-slider"
           data-mode="${_esc(modeId)}" data-svc="${_esc(svcName)}"
           min="0" max="${_esc(budget)}" step="0.5" value="${_esc(current)}" />
         <span class="slider-val alloc-val">${_esc(current)} GB</span>
       </div>
       <div class="config-sub alloc-projection" id="alloc-proj-${_esc(modeId)}"></div>`;
    body.appendChild(row);
    const slider = row.querySelector(".alloc-slider");
    const label  = row.querySelector(".alloc-val");
    slider.addEventListener("input", () => { label.textContent = slider.value + " GB"; });
    slider.addEventListener("change", async () => {
      const r = await _fetch(
        `${OLLMO_API}/modes/${modeId}/allocations/${encodeURIComponent(svcName)}`,
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ gb: parseFloat(slider.value) }) }
      );
      const data = await r.json();
      const proj = row.querySelector(".alloc-projection");
      proj.textContent = data.projected_gb != null
        ? `Mode total: ~${data.projected_gb} / ${data.budget_gb} GB`
        : "";
    });
  }
}

// --- Per-node routing config ---
// Maps service name → its known route fields
const SERVICE_ROUTES = {
  ollama: [
    { key: "ollama_url", label: "Ollama API", hint: "LLM inference endpoint", default: "http://ollama:11434" },
  ],
  "open-webui": [
    { key: "ollama_url", label: "LLM Backend", hint: "Ollama URL this UI connects to", default: "http://ollama:11434" },
    { key: "tts_url", label: "TTS Endpoint", hint: "Text-to-speech API (OpenAI-compat)", default: "http://rvc:8001/v1" },
  ],
  "kokoro-tts": [
    { key: "tts_url", label: "TTS Serve URL", hint: "Kokoro ONNX endpoint", default: "http://kokoro-tts:8000" },
  ],
  rvc: [
    { key: "tts_url", label: "RVC Proxy", hint: "Voice conversion proxy (OpenAI-compat)", default: "http://rvc:8001/v1" },
    { key: "rvc_upstream", label: "TTS Upstream", hint: "TTS source that feeds into RVC", default: "http://kokoro-tts:8000" },
  ],
  "f5-tts": [
    { key: "f5_url", label: "F5-TTS Gradio", hint: "Voice cloning Gradio API", default: "http://f5-tts:7860" },
  ],
  comfyui: [
    { key: "image_gen_url", label: "ComfyUI API", hint: "Image generation endpoint", default: "http://comfyui:8188" },
  ],
};

async function loadNodeRouting(node) {
  const body = document.getElementById("node-routing-body");
  const svcName = node._svc?.name;
  if (!svcName) {
    body.innerHTML = '<div class="dim" style="font-size:9px">Select a service node</div>';
    return;
  }

  const routes = SERVICE_ROUTES[svcName];
  if (!routes || routes.length === 0) {
    body.innerHTML = '<div class="dim" style="font-size:9px">No routes configured for this service</div>';
    return;
  }

  // Load current routing config
  let routing = {};
  try {
    const r = await _fetch(`${OLLMO_API}/config/routing`);
    routing = await r.json();
  } catch {}

  body.innerHTML = "";
  routes.forEach(rt => {
    const entry = document.createElement("div");
    entry.className = "route-entry";
    const val = routing[rt.key] || rt.default || "";
    entry.innerHTML =
      `<label>${_esc(rt.label)}</label>
       <input type="text" value="${_esc(val)}" data-route-key="${_esc(rt.key)}" />
       <span class="route-hint">${_esc(rt.hint)}</span>`;
    body.appendChild(entry);

    const input = entry.querySelector("input");
    input.addEventListener("change", async () => {
      await _fetch(`${OLLMO_API}/config/routing`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [rt.key]: input.value }),
      });
      // Sync canvas wires to reflect updated routing
      syncWiresFromRouting();
    });
  });
}

// --- Node config sub-settings ---
function updateNodeSubs(saved) {
  updateMemorySub(saved);
  updatePrioritySub();
  updateBusSub();
  updateLimitSub(saved);
  updateBootSub(saved);
}

function setSubHtml(id, html) {
  const el = document.getElementById(id);
  if (!html) { el.innerHTML = ""; el.classList.add("hidden"); return; }
  el.innerHTML = html;
  el.classList.remove("hidden");
}

function updateMemorySub(saved) {
  const val = document.getElementById("cfg-memory").value;
  if (val === "vram") {
    const alloc = saved?.vramAlloc || 4;
    setSubHtml("sub-memory",
      `<label>VRAM allocation</label>
       <div style="display:flex;align-items:center;gap:6px">
         <input type="range" id="sub-vram-alloc" min="0.5" max="20" step="0.5" value="${alloc}" />
         <span class="slider-val" id="sub-vram-alloc-val">${alloc} GB</span>
       </div>`
    );
    const sl = document.getElementById("sub-vram-alloc");
    const lbl = document.getElementById("sub-vram-alloc-val");
    sl.addEventListener("input", () => { lbl.textContent = sl.value + " GB"; saveNodeConfig(); });
  } else if (val === "ram") {
    const lim = saved?.ramLimit || 8;
    setSubHtml("sub-memory",
      `<label>RAM limit</label>
       <div style="display:flex;align-items:center;gap:6px">
         <input type="range" id="sub-ram-limit" min="1" max="62" step="1" value="${lim}" />
         <span class="slider-val" id="sub-ram-limit-val">${lim} GB</span>
       </div>`
    );
    const sl = document.getElementById("sub-ram-limit");
    const lbl = document.getElementById("sub-ram-limit-val");
    sl.addEventListener("input", () => { lbl.textContent = sl.value + " GB"; saveNodeConfig(); });
  } else if (val === "storage") {
    const p = saved?.swapPath || "/mnt/storage/swap";
    setSubHtml("sub-memory",
      `<label>Swap file path</label>
       <input type="text" id="sub-swap-path" value="${p}"
         style="background:var(--bg);border:1px solid var(--border);color:var(--text);
                padding:3px 6px;border-radius:3px;font-family:inherit;font-size:10px;width:160px" />`
    );
    document.getElementById("sub-swap-path").addEventListener("change", saveNodeConfig);
  } else {
    setSubHtml("sub-memory", "");
  }
}

const PRIORITY_DESC = [
  "", "Protected — last to close", "High — yields only to protected",
  "Normal — balanced scheduling", "Low — yields to higher priority",
  "Hot swap — first to park",
];

function updatePrioritySub() {
  const val = parseInt(document.getElementById("cfg-priority").value) || 1;
  const el = document.getElementById("cfg-priority-desc");
  if (el) el.textContent = PRIORITY_DESC[val] || "";
}

const BUS_DESC = { nvme: "Est. load: ~1.5 s", sata: "Est. load: ~13 s", ram: "Est. load: ~0.1 s" };

function updateBusSub() {
  const val = document.getElementById("cfg-bus").value;
  setSubHtml("sub-bus", `<span class="sub-desc">${BUS_DESC[val] || ""}</span>`);
}

function updateLimitSub(saved) {
  const val = document.getElementById("cfg-limit").value;
  if (val === "soft") {
    setSubHtml("sub-limit", `<span class="sub-desc">Action: warn only</span>`);
  } else {
    const act = saved?.limitAct || "swap";
    setSubHtml("sub-limit",
      `<label>On exceed</label>
       <select id="sub-limit-action"
         style="background:var(--bg3);border:1px solid var(--border);color:var(--text);
                padding:3px 6px;border-radius:3px;font-family:inherit;font-size:10px">
         <option value="kill"  ${act==="kill"  ? "selected":""}>Kill process</option>
         <option value="swap"  ${act==="swap"  ? "selected":""}>Swap to RAM</option>
         <option value="pause" ${act==="pause" ? "selected":""}>Pause</option>
       </select>`
    );
    document.getElementById("sub-limit-action").addEventListener("change", saveNodeConfig);
  }
}

function updateBootSub(saved) {
  const checked = document.getElementById("cfg-boot").checked;
  if (checked) {
    const delay = saved?.bootDelay || 0;
    setSubHtml("sub-boot",
      `<label>Boot delay</label>
       <div style="display:flex;align-items:center;gap:6px">
         <input type="number" id="sub-boot-delay" min="0" max="120" value="${delay}"
           style="background:var(--bg3);border:1px solid var(--border);color:var(--text);
                  padding:3px 6px;border-radius:3px;font-family:inherit;font-size:10px;width:52px" />
         <span class="sub-desc">seconds</span>
       </div>`
    );
    document.getElementById("sub-boot-delay").addEventListener("change", saveNodeConfig);
  } else {
    setSubHtml("sub-boot", "");
  }
}

// --- Mode-level config ---
let modeConfigs  = {};

// --- Paths & Routing panel (config-panel, hidden in Tab 2) ---
async function loadPathsPanel() {
  showConfigView("paths");
  document.getElementById("config-node-name").textContent  = "Storage Paths";
  document.getElementById("config-node-group").textContent = "symlinks + routing";
  document.getElementById("config-vram-est").textContent   = "";
  try {
    const [pathsR, routingR] = await Promise.all([
      _fetch(`${OLLMO_API}/config/paths`),
      _fetch(`${OLLMO_API}/config/routing`),
    ]);
    const paths   = await pathsR.json();
    const routing = await routingR.json();
    renderPathsList(paths);
    document.getElementById("route-tts-url").value    = routing.tts_url        || "";
    document.getElementById("route-imggen-url").value = routing.image_gen_url  || "";
    document.getElementById("route-ollama-url").value = routing.ollama_url     || "";
    document.getElementById("route-stt-url").value    = routing.stt_url        || "";
  } catch (e) {
    console.warn("loadPathsPanel error:", e);
  }
}

// Shared path renderer — used by config panel (Tab 2) and Advanced tab
function renderPathsInto(paths, listEl, onRefresh) {
  listEl.innerHTML = "";
  paths.forEach(p => {
    const entry = document.createElement("div");
    entry.className = "path-entry";
    entry.dataset.name = p.name;
    const statusDot  = `<span class="path-status ${p.exists ? 'ok' : 'missing'}" title="${p.exists ? 'target exists' : 'target missing'}"></span>`;
    const tierBadge  = `<span class="tier-badge ${_esc(p.tier)}">${_esc(p.tier)}</span>`;
    const ctrs       = (p.containers || []).map(c => `<span class="ctr-badge">${_esc(c)}</span>`).join("");
    const targetText = _esc(p.target || '—');
    entry.innerHTML =
      `<div class="path-row-main">` +
        statusDot +
        `<span class="path-entry-label">${_esc(p.label)}</span>` +
        tierBadge +
        `<span class="path-entry-target" title="${_esc(p.target || '')}">${targetText}</span>` +
        `<button class="path-edit-btn" data-name="${_esc(p.name)}">EDIT</button>` +
        `<button class="path-del-btn" data-name="${_esc(p.name)}" title="Remove">✕</button>` +
      `</div>` +
      (ctrs ? `<div class="path-ctrs">${ctrs}</div>` : "");
    entry.querySelector(".path-edit-btn").addEventListener("click", () =>
      startPathEdit(entry, p.name, p.target || p.default_target, onRefresh)
    );
    entry.querySelector(".path-del-btn").addEventListener("click", async () => {
      if (!confirm(`Remove path "${p.label}"?`)) return;
      await _fetch(`${OLLMO_API}/config/paths/${encodeURIComponent(p.name)}`, { method: "DELETE" });
      onRefresh();
    });
    listEl.appendChild(entry);
  });
  const addBtn = document.createElement("button");
  addBtn.className = "path-add-btn mode-btn";
  addBtn.textContent = "+ Add Path";
  addBtn.addEventListener("click", () => showAddPathForm(listEl, addBtn, onRefresh));
  listEl.appendChild(addBtn);
}

function renderPathsList(paths) {
  renderPathsInto(paths, document.getElementById("paths-list"), loadPathsPanel);
}

function showAddPathForm(list, addBtn, onRefresh) {
  addBtn.remove();
  const form = document.createElement("div");
  form.className = "path-add-form";
  form.innerHTML =
    `<input class="path-add-input" id="add-name"   placeholder="name (e.g. my-models)" />` +
    `<input class="path-add-input" id="add-label"  placeholder="label (e.g. My Models)" />` +
    `<input class="path-add-input" id="add-target" placeholder="/path/to/directory" />` +
    `<div style="display:flex;gap:6px;margin-top:4px">` +
      `<button class="mode-btn" id="add-confirm">ADD</button>` +
      `<button class="mode-btn" id="add-cancel">CANCEL</button>` +
    `</div>`;
  list.appendChild(form);
  document.getElementById("add-cancel").addEventListener("click", onRefresh);
  document.getElementById("add-confirm").addEventListener("click", async () => {
    const name   = document.getElementById("add-name").value.trim();
    const label  = document.getElementById("add-label").value.trim();
    const target = document.getElementById("add-target").value.trim();
    if (!name || !target) return;
    await _fetch(`${OLLMO_API}/config/paths`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, label: label || name, target }),
    });
    onRefresh();
  });
}

function startPathEdit(entry, name, currentTarget, onRefresh) {
  const targetEl = entry.querySelector(".path-entry-target");
  const editBtn  = entry.querySelector(".path-edit-btn");
  const input = document.createElement("input");
  input.className = "path-edit-input";
  input.value = currentTarget || "";
  targetEl.replaceWith(input);
  editBtn.textContent = "SAVE";
  input.focus();
  const save = async () => {
    const newTarget = input.value.trim();
    if (!newTarget) { onRefresh(); return; }
    try {
      await _fetch(`${OLLMO_API}/config/paths/${encodeURIComponent(name)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: newTarget }),
      });
    } catch (e) { console.warn("repoint error:", e); }
    onRefresh();
  };
  editBtn.onclick = save;
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") save();
    if (e.key === "Escape") onRefresh();
  });
}

document.getElementById("config-paths-btn").addEventListener("click", loadPathsPanel);

document.getElementById("save-routing-btn").addEventListener("click", async () => {
  const btn  = document.getElementById("save-routing-btn");
  const body = {
    tts_url:       document.getElementById("route-tts-url").value.trim(),
    image_gen_url: document.getElementById("route-imggen-url").value.trim(),
    ollama_url:    document.getElementById("route-ollama-url").value.trim(),
    stt_url:       document.getElementById("route-stt-url").value.trim(),
  };
  try {
    await _fetch(`${OLLMO_API}/config/routing`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    btn.textContent = "SAVED";
    // Sync canvas wires to match new routing
    syncWiresFromRouting();
  } catch (e) {
    console.warn("save routing error:", e);
    btn.textContent = "ERROR";
  }
  setTimeout(() => { btn.textContent = "APPLY ROUTING"; }, 2000);
});

// --- Live tab card renderers ---

// Benchmark card
const _benchHistory  = [];
const _benchColors   = {};
function _getBenchPalette() {
  const cs = getComputedStyle(document.documentElement);
  const v = n => cs.getPropertyValue(n).trim();
  return [
    v("--grp-llm") || "#42a5f5", v("--grp-audio") || "#ffa726",
    v("--grp-render") || "#66bb6a", v("--tier-vram") || "#ab47bc",
    v("--grp-control") || "#78909c", v("--mode-ollmo") || "#e879f9",
    v("--tier-sata") || "#ffa726", v("--green") || "#00e676",
    v("--cyan") || "#00d2be", v("--mode-comfyui") || "#facc15",
  ];
}
let _benchColorIdx = 0;

function _benchColor(name) {
  if (!_benchColors[name]) {
    const pal = _getBenchPalette();
    _benchColors[name] = pal[_benchColorIdx % pal.length];
    _benchColorIdx++;
  }
  return _benchColors[name];
}

// Last known benchmark args for redraw on resize
let _lastBenchArgs = { loaded: [], vram: null, gpu: null, ram: null };

function _redrawBench() {
  const a = _lastBenchArgs;
  if (a.vram || a.gpu || a.ram) renderBenchmark(a.loaded, a.vram, a.gpu, a.ram, true);
}

function renderBenchmark(loaded, vram, gpu, ram, resizeOnly) {
  const modelRow = document.getElementById("bench-model-row");
  const canvas   = document.getElementById("bench-canvas");
  const legend   = document.getElementById("bench-legend");
  if (!modelRow || !canvas || !legend) return;

  const vramUsed = (vram?.used_gb ?? parseFloat(document.getElementById("vram-label")?.textContent)) || 0;
  const gpuPct   = (gpu?.gpu_use_percent) ?? 0;
  const ramUsed  = ram?.used_gb || 0;
  const maxRam   = ram?.total_gb || 62;
  const maxVram  = _enfData?.effective_total_gb || _enfData?.real_total_gb || 21;

  _lastBenchArgs = { loaded, vram, gpu, ram };

  if (!resizeOnly) {
    const modelName = loaded && loaded.length > 0 ? loaded[0].name : null;
    const now = new Date();
    const tsLabel = now.toTimeString().slice(0, 8);
    const prev = _benchHistory[_benchHistory.length - 1];
    const swapped = prev && prev.model !== modelName;
    _benchHistory.push({ vram: vramUsed, gpu: gpuPct, ram: ramUsed, maxRam, model: modelName, ts: tsLabel, swap: swapped });
    if (_benchHistory.length > 300) _benchHistory.shift();

    const modelChips = (!loaded || loaded.length === 0)
      ? '<span class="dim">No model loaded</span>'
      : loaded.map(m => {
          const shortName = m.name.split("/").pop().split(":")[0];
          const color = _benchColor(m.name);
          return `<span class="bench-model-chip active" style="border-color:${color};color:${color}">${shortName}</span>`;
        }).join("");
    modelRow.innerHTML =
      `<span class="bench-stats">VRAM <b>${vramUsed.toFixed(1)}</b> / ${maxVram.toFixed(1)} GB</span>` +
      `<span class="bench-stats-sep">|</span>` +
      `<span class="bench-stats">GPU <b>${gpuPct.toFixed(0)}</b>%</span>` +
      `<span class="bench-stats-sep">|</span>` +
      `<span class="bench-stats">RAM <b>${ramUsed.toFixed(1)}</b> / ${maxRam.toFixed(0)} GB</span>` +
      `<span class="bench-stats-sep">|</span>` +
      modelChips;
  }

  // Draw graph
  const W = canvas.offsetWidth || 400;
  const H = canvas.offsetHeight || 120;
  canvas.width  = W * devicePixelRatio;
  canvas.height = H * devicePixelRatio;
  canvas.style.width  = W + "px";
  canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  ctx.clearRect(0, 0, W, H);

  // Read tier colors from CSS vars (responds to settings)
  const cs = getComputedStyle(document.documentElement);
  const cVram = cs.getPropertyValue("--tier-vram").trim() || "#ab47bc";
  const cGpu  = cs.getPropertyValue("--grp-control").trim() || "#78909c";
  const cRam  = cs.getPropertyValue("--tier-ram").trim() || "#00e676";

  if (_benchHistory.length < 2) {
    ctx.fillStyle = cs.getPropertyValue("--text-dim").trim() || "#555";
    ctx.font = "11px monospace";
    ctx.textAlign = "center";
    ctx.fillText("Waiting for data\u2026", W / 2, H / 2);
    return;
  }

  const padL = 32, padR = 28, padB = 16, padT = 4;
  const chartW = W - padL - padR;
  const chartH = H - padT - padB;

  const pts = _benchHistory.slice(-Math.min(_benchHistory.length, chartW));
  const step = pts.length > 1 ? chartW / (pts.length - 1) : chartW;

  const yVram = v => padT + chartH - (v / maxVram) * chartH;
  const yGpu  = v => padT + chartH - (v / 100) * chartH;
  const xPt   = i => padL + i * step;

  // Grid lines
  ctx.strokeStyle = cVram + "10";
  ctx.lineWidth = 1;
  for (const frac of [0.25, 0.5, 0.75]) {
    const y = padT + chartH - frac * chartH;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + chartW, y); ctx.stroke();
  }
  for (let i = 0; i < pts.length; i++) {
    if (i > 0 && i % 60 === 0) {
      const x = xPt(i);
      ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, padT + chartH); ctx.stroke();
    }
  }

  // VRAM filled area
  ctx.beginPath();
  ctx.moveTo(xPt(0), yVram(pts[0].vram));
  for (let i = 1; i < pts.length; i++) ctx.lineTo(xPt(i), yVram(pts[i].vram));
  ctx.lineTo(xPt(pts.length - 1), padT + chartH);
  ctx.lineTo(xPt(0), padT + chartH);
  ctx.closePath();
  ctx.fillStyle = cVram + "26";
  ctx.fill();

  // VRAM line
  ctx.beginPath();
  ctx.moveTo(xPt(0), yVram(pts[0].vram));
  for (let i = 1; i < pts.length; i++) ctx.lineTo(xPt(i), yVram(pts[i].vram));
  ctx.strokeStyle = cVram; ctx.lineWidth = 1.5; ctx.stroke();

  // GPU line
  ctx.beginPath();
  ctx.moveTo(xPt(0), yGpu(pts[0].gpu || 0));
  for (let i = 1; i < pts.length; i++) ctx.lineTo(xPt(i), yGpu(pts[i].gpu || 0));
  ctx.strokeStyle = cGpu; ctx.lineWidth = 1.5; ctx.stroke();

  // RAM line
  const yRam = v => {
    const mR = pts[0].maxRam || maxRam;
    return padT + chartH - (v / mR) * chartH;
  };
  ctx.beginPath();
  ctx.moveTo(xPt(0), yRam(pts[0].ram || 0));
  for (let i = 1; i < pts.length; i++) ctx.lineTo(xPt(i), yRam(pts[i].ram || 0));
  ctx.strokeStyle = cRam; ctx.lineWidth = 1.5; ctx.stroke();

  // Swap markers
  ctx.font = "7px monospace";
  for (let i = 0; i < pts.length; i++) {
    if (!pts[i].swap) continue;
    const x = xPt(i);
    ctx.strokeStyle = _benchColor(pts[i].model || "__idle__");
    ctx.lineWidth = 1; ctx.setLineDash([2, 2]);
    ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, padT + chartH); ctx.stroke();
    ctx.setLineDash([]);
  }

  // Y-axis labels
  ctx.fillStyle = cVram + "99"; ctx.font = "8px monospace"; ctx.textAlign = "right";
  ctx.fillText(maxVram.toFixed(0) + "G", padL - 3, padT + 8);
  ctx.fillText((maxVram / 2).toFixed(0) + "G", padL - 3, padT + chartH / 2 + 3);
  ctx.fillText("0", padL - 3, padT + chartH - 2);

  ctx.fillStyle = cGpu + "99"; ctx.textAlign = "left";
  ctx.fillText("100%", padL + chartW + 3, padT + 8);
  ctx.fillText("50%", padL + chartW + 3, padT + chartH / 2 + 3);
  ctx.fillText("0%", padL + chartW + 3, padT + chartH - 2);

  // X-axis time labels
  const cText = cs.getPropertyValue("--text").trim() || "#e8e8e8";
  ctx.fillStyle = cText + "44"; ctx.textAlign = "center"; ctx.font = "7px monospace";
  for (let i = 0; i < pts.length; i++) {
    if (i > 0 && i % 60 === 0) ctx.fillText(pts[i].ts.slice(0, 5), xPt(i), H - 2);
  }

  // Legend
  if (!resizeOnly) {
    const seen = new Set();
    const modelItems = _benchHistory.filter(p => {
      if (!p.model || seen.has(p.model)) return false;
      seen.add(p.model); return true;
    }).map(p => p.model);
    legend.innerHTML =
      `<span class="bench-legend-item"><span class="bench-legend-dot" style="background:${cVram}"></span>VRAM</span>` +
      `<span class="bench-legend-item"><span class="bench-legend-dot" style="background:${cGpu}"></span>GPU</span>` +
      `<span class="bench-legend-item"><span class="bench-legend-dot" style="background:${cRam}"></span>RAM</span>` +
      modelItems.map(name => {
        const label = name.split("/").pop().split(":")[0];
        return `<span class="bench-legend-item"><span class="bench-legend-dot" style="background:${_benchColor(name)}"></span>${label}</span>`;
      }).join("") +
      (modelItems.length === 0 && _benchHistory.length < 2 ? '<span class="dim" style="font-size:8px">No data yet</span>' : "");
  }
}

// Accounting strip (topbar #acct-strip)
function renderAccounting(acct) {
  if (!acct) return;
  const ids = {
    "acct-vram-ext":  (acct.vram_external?.toFixed(1)  ?? "0.0") + " GB",
    "acct-vram-free": (acct.vram_headroom?.toFixed(1)  ?? "?")   + " GB",
    "acct-ram-ext":   (acct.ram_external?.toFixed(1)   ?? "0.0") + " GB",
    "acct-ram-free":  (acct.ram_headroom?.toFixed(1)   ?? "?")   + " GB",
  };
  for (const [id, val] of Object.entries(ids)) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  }
}

// Kill log card
function renderKillLog(events) {
  const el = document.getElementById("killlog-list");
  if (!el) return;
  if (!events || events.length === 0) {
    el.innerHTML = '<span class="dim">No events</span>';
    return;
  }
  const icons = { kill: "✕", restore: "↺", crash: "!" };
  const now = Date.now() / 1000;
  el.innerHTML = [...events].reverse().slice(0, 10).map(ev => {
    const ago = formatAgo(now - (ev.ts || 0));
    return `<div class="ev-row ev-${ev.event}">
      <span class="ev-icon">${icons[ev.event] || "?"}</span>
      <span class="ev-svc">${ev.service || ev.container || "?"}</span>
      <span class="ev-type">${ev.event}</span>
      <span class="ev-vram">${ev.vram_used != null ? ev.vram_used.toFixed(1) + 'GB' : '?'}</span>
      <span class="ev-ago">${ago}</span>
    </div>`;
  }).join("");
}

function formatAgo(secs) {
  if (secs < 60) return `${Math.round(secs)}s`;
  if (secs < 3600) return `${Math.round(secs / 60)}m`;
  return `${Math.round(secs / 3600)}h`;
}

// Enforcement card
let _enfData = {};
let _enfRealTotal = 20;

async function enfToggle(enabled) {
  try {
    await _fetch(`${OLLMO_API}/enforcement/${enabled ? 'enable' : 'disable'}`, { method: "POST" });
  } catch {}
}

async function enfSetCeiling() {
  const input = document.getElementById("enf-ceiling-input");
  const warnEl = document.getElementById("enf-ceiling-warn");
  const val = parseFloat(input.value);

  if (isNaN(val) || val < 0) {
    input.value = "";
    // Treat as clear
    enfClearCeiling();
    return;
  }

  // Validate
  warnEl.classList.add("hidden");
  if (val > _enfRealTotal) {
    warnEl.textContent = `Ceiling ${val}GB exceeds real VRAM (${_enfRealTotal}GB) — enforcer will never trigger`;
    warnEl.classList.remove("hidden");
  }

  try {
    await _fetch(`${OLLMO_API}/enforcement/ceiling`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vram_ceiling_gb: val }),
    });
    input.classList.add("has-ceiling");
  } catch {}
}

async function enfClearCeiling() {
  const input = document.getElementById("enf-ceiling-input");
  const warnEl = document.getElementById("enf-ceiling-warn");
  try {
    await _fetch(`${OLLMO_API}/enforcement/ceiling`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vram_ceiling_gb: null }),
    });
    input.value = "";
    input.classList.remove("has-ceiling");
    warnEl.classList.add("hidden");
  } catch {}
}

document.getElementById("enf-ceiling-apply")?.addEventListener("click", enfSetCeiling);
document.getElementById("enf-ceiling-clear")?.addEventListener("click", enfClearCeiling);
document.getElementById("enf-ceiling-input")?.addEventListener("keydown", e => {
  if (e.key === "Enter") enfSetCeiling();
});

async function svcSetLimitMode(name, mode) {
  try {
    await _fetch(`${OLLMO_API}/config/services/${encodeURIComponent(name)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit_mode: mode }),
    });
    await fetchServicesCfg();
  } catch {}
}

function renderEnforcement(data) {
  if (!data) return;
  _enfData = data;

  if (data.real_total_gb) _enfRealTotal = data.real_total_gb;

  const cb = document.getElementById("enf-master-cb");
  if (cb && cb !== document.activeElement) cb.checked = data.enabled !== false;

  const statusEl = document.getElementById("enf-status-val");
  if (statusEl) {
    const enabled = data.enabled !== false;
    const paused  = !data.active_modes?.length;
    let label, cls;
    if (!enabled)      { label = "disabled"; cls = "dim"; }
    else if (paused)   { label = "paused · no active mode"; cls = "dim"; }
    else if (data.enforcing) { label = "ENFORCING"; cls = "enf-hot"; }
    else               { label = "armed"; cls = "enf-ok"; }
    statusEl.textContent = label;
    statusEl.className   = "enf-val " + cls;
  }

  // Sync ceiling input (only when not focused)
  const ceilInput = document.getElementById("enf-ceiling-input");
  if (ceilInput && ceilInput !== document.activeElement) {
    if (data.vram_ceiling_gb != null && data.vram_ceiling_gb > 0) {
      ceilInput.value = data.vram_ceiling_gb;
      ceilInput.classList.add("has-ceiling");
    } else {
      ceilInput.value = "";
      ceilInput.placeholder = data.real_total_gb ? `${data.real_total_gb}` : "—";
      ceilInput.classList.remove("has-ceiling");
    }
  }

  const effTotal = data.effective_total_gb || data.real_total_gb || 20;
  const warnEl = document.getElementById("enf-warn-val");
  if (warnEl) warnEl.textContent = data.warn_at_gb != null ? `${data.warn_at_gb} GB (${Math.round(data.warn_threshold * 100)}%)` : "—";

  const hardEl = document.getElementById("enf-hard-val");
  if (hardEl) hardEl.textContent = data.hard_at_gb != null ? `${data.hard_at_gb} GB (${Math.round(data.hard_threshold * 100)}%)` : "—";

}

async function svcSetPriority(name, val) {
  const priority = Math.max(1, Math.min(5, parseInt(val) || 3));
  try {
    await _fetch(`${OLLMO_API}/config/services/${encodeURIComponent(name)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ priority }),
    });
    await fetchServicesCfg();
  } catch {}
}

// Services card
let _svcsCfg = {};

async function fetchServicesCfg() {
  try {
    const r = await _fetch(`${OLLMO_API}/config/services`);
    _svcsCfg = await r.json();
  } catch {}
}

function renderServices(services, killOrder) {
  const el = document.getElementById("services-list");
  if (!el) return;
  if (!services || services.length === 0) {
    el.innerHTML = '<span class="dim">No services</span>';
    return;
  }
  // Build priority lookup from kill_order
  const koMap = {};
  if (killOrder) killOrder.forEach(k => { koMap[k.service] = k; });

  // Sort by priority ascending (1=protected at top, 5=killed first at bottom)
  const sorted = [...services].sort((a, b) => {
    const pa = koMap[a.name]?.priority ?? (_svcsCfg[a.name]?.priority ?? 3);
    const pb = koMap[b.name]?.priority ?? (_svcsCfg[b.name]?.priority ?? 3);
    return pa - pb;
  });

  el.innerHTML = sorted.map((svc, i) => {
    const cfg     = _svcsCfg[svc.name] || {};
    const ko      = koMap[svc.name] || {};
    const vram    = cfg.vram_est_gb || 0;
    const st      = svc.status || "unknown";
    const dot     = st === "running" ? "ok" : (st === "exited" || st === "not_found") ? "err" : "warn";
    const running = st === "running";
    const restore   = cfg.auto_restore !== false;
    const limitMode = cfg.limit_mode || "soft";
    const limitCls  = limitMode === "hard" ? "lm-hard" : limitMode === "off" ? "lm-off" : "lm-soft";
    const priority  = ko.priority ?? cfg.priority ?? 3;
    const wouldKill = ko.would_kill ? " ko-would-kill" : "";
    const boot      = !!cfg.boot_with_system;
    const eName = _esc(svc.name);
    return `<div class="svc-row${wouldKill}">
      <span class="enf-ko-pos">${sorted.length - i}</span>
      <span class="svc-dot ${dot}"></span>
      <span class="svc-name">${eName}</span>
      <span class="svc-lm ${limitCls} svc-lm-btn" title="Limit mode — click to cycle"
        data-svc="${eName}" data-lm="${_esc(limitMode)}">${_esc(limitMode)}</span>
      <span class="svc-status">${_esc(st)}</span>
      <span class="svc-vram">${vram > 0 ? vram + 'GB' : '—'}</span>
      <input type="number" class="enf-ko-prio svc-prio-input" data-svc="${eName}" value="${priority}" min="1" max="5" title="Priority (1=protected, 5=first killed)">
      <button class="svc-btn svc-start-btn" title="Start" ${running ? 'disabled' : ''}
        data-svc="${eName}">▶</button>
      <button class="svc-btn svc-stop-btn" title="Stop" ${!running ? 'disabled' : ''}
        data-svc="${eName}">■</button>
      <label class="svc-restore-toggle" title="${restore ? 'Auto-restore on' : 'Auto-restore off'}">
        <input type="checkbox" class="svc-restore-cb" data-svc="${eName}" ${restore ? 'checked' : ''}>
        <span class="svc-restore-track"></span>
      </label>
      <span class="svc-boot-pill${boot ? ' boot-on' : ''}" data-svc="${eName}" data-boot="${boot ? 'false' : 'true'}" title="${boot ? 'Boot with system — click to disable' : 'Not booting with system — click to enable'}">BOOT</span>
    </div>`;
  }).join("");

  // Wire up event listeners (avoid inline onclick/onchange with user data)
  el.querySelectorAll('.svc-lm-btn').forEach(btn => btn.addEventListener('click', () => svcCycleLimitMode(btn.dataset.svc, btn.dataset.lm)));
  el.querySelectorAll('.svc-prio-input').forEach(inp => inp.addEventListener('change', () => svcSetPriority(inp.dataset.svc, inp.value)));
  el.querySelectorAll('.svc-start-btn').forEach(btn => btn.addEventListener('click', () => svcAction(btn.dataset.svc, 'start')));
  el.querySelectorAll('.svc-stop-btn').forEach(btn => btn.addEventListener('click', () => svcAction(btn.dataset.svc, 'stop')));
  el.querySelectorAll('.svc-restore-cb').forEach(cb => cb.addEventListener('change', () => svcToggleRestore(cb.dataset.svc, cb.checked)));
  el.querySelectorAll('.svc-boot-pill').forEach(btn => btn.addEventListener('click', () => svcToggleBoot(btn.dataset.svc, btn.dataset.boot === 'true')));
}

async function svcAction(name, action) {
  try {
    await _fetch(`${OLLMO_API}/services/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    setTimeout(poll, 1500);
  } catch {}
}

async function svcToggleRestore(name, enabled) {
  try {
    await _fetch(`${OLLMO_API}/config/services/${encodeURIComponent(name)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_restore: enabled }),
    });
    if (_svcsCfg[name]) _svcsCfg[name].auto_restore = enabled;
  } catch {}
}

async function svcToggleBoot(name, enabled) {
  try {
    await _fetch(`${OLLMO_API}/config/services/${encodeURIComponent(name)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ boot_with_system: enabled }),
    });
    if (_svcsCfg[name]) _svcsCfg[name].boot_with_system = enabled;
    setTimeout(poll, 500);
  } catch {}
}

async function svcCycleLimitMode(name, current) {
  const cycle = { soft: "hard", hard: "off", off: "soft" };
  const next  = cycle[current] || "soft";
  await svcSetLimitMode(name, next);
}

// RAM tier card
let _pathsCache = [];
let _lastRamTier = null;

async function fetchPathsForRamTier() {
  try {
    const r = await _fetch(`${OLLMO_API}/config/paths`);
    _pathsCache = await r.json();
    renderRamTier(_lastRamTier);
    renderDiskMap(_pathsCache);
  } catch {}
}

let _rtRefreshPending = false;
function renderRamTier(ramTier) {
  const changed = JSON.stringify(_lastRamTier) !== JSON.stringify(ramTier);
  _lastRamTier = ramTier;
  if (changed && ramTier !== null && !_rtRefreshPending) {
    _rtRefreshPending = true;
    fetchPathsForRamTier().finally(() => { _rtRefreshPending = false; });
  }
  const el = document.getElementById("ramtier-content");
  if (!el) return;

  let headerHTML;
  if (ramTier && Object.keys(ramTier).length > 0 && ramTier.pools) {
    const poolCount = Object.keys(ramTier.pools).length;
    const used = ramTier.used_gb ?? 0;
    const ceiling = ramTier.ceiling_gb ?? 0;
    const free = ramTier.free_gb ?? 0;
    const pct = ramTier.percent ?? 0;
    const pctNorm = pct / 100;
    const barCls = pctNorm > 0.80 ? " hot" : pctNorm > 0.50 ? " warn" : "";
    headerHTML = `<div class="acct-stats" style="margin-bottom:4px">
      <span>${poolCount} pool${poolCount !== 1 ? 's' : ''} active</span>
      <span>${_esc(used.toFixed(1))} / ${_esc(String(ceiling))} GB (${_esc(pct.toFixed(0))}%)</span>
    </div>
    <div class="rt-bar-wrap">
      <div class="rt-bar"><div class="fill${barCls}" style="width:${Math.min(pct, 100)}%"></div></div>
      <div class="rt-bar-labels">
        <span class="rt-used">${_esc(used.toFixed(2))} GB used</span>
        <span class="rt-free">${_esc(free.toFixed(2))} GB free</span>
      </div>
    </div>`;
  } else {
    headerHTML = `<div class="dim" style="margin-bottom:8px; font-size:10px">0 pools active &mdash; toggle paths below to pin to RAM</div>`;
  }

  let anyRam = false;
  const sortedPaths = [..._pathsCache].sort((a, b) => (b.tier === "ram" ? 1 : 0) - (a.tier === "ram" ? 1 : 0));
  const pathRows = sortedPaths.map(p => {
    const isRam = p.tier === "ram";
    if (isRam) anyRam = true;
    const diskTier = p.default_tier?.toUpperCase() || p.tier_default?.toUpperCase() || "NVME";
    const tierCls = diskTier === "SATA" ? "sata" : diskTier === "RAM" ? "ram" : "nvme";
    const poolSize = (isRam && ramTier?.pools?.[p.name] != null)
      ? `<span class="rt-pool-size">${ramTier.pools[p.name].toFixed(2)} GB</span>`
      : '';
    return `<div class="rt-row">
      <span class="rt-name">${_esc(p.label || p.name)}</span>
      ${poolSize}
      <span class="rt-tier-label tier-${_esc(tierCls)}${isRam ? ' active' : ''}">${isRam ? _esc(diskTier) + ' \u2192 RAM' : _esc(diskTier)}</span>
      <label class="rt-toggle" title="${isRam ? 'Move back to ' + _esc(diskTier) : 'Move to RAM'}">
        <input type="checkbox" class="rt-path-cb" data-path="${_esc(p.name)}" ${isRam ? 'checked' : ''}>
        <span class="rt-toggle-track"></span>
      </label>
    </div>`;
  }).join("");

  el.innerHTML = headerHTML + pathRows;

  // Wire up RAM tier toggle listeners (avoid inline onchange with user data)
  el.querySelectorAll('.rt-path-cb').forEach(cb => cb.addEventListener('change', () => rtTogglePath(cb.dataset.path, cb.checked)));

  // Sync master toggle
  const masterCb = document.getElementById("rt-master-cb");
  if (masterCb) masterCb.checked = anyRam;
}

async function rtTogglePath(name, toRam) {
  try {
    await _fetch(`${OLLMO_API}/config/paths/${encodeURIComponent(name)}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: toRam ? "ram" : "default" }),
    });
    fetchPathsForRamTier();
  } catch {}
}

async function rtMasterToggle(enabled) {
  const target = enabled ? "ram" : "default";
  try {
    await Promise.all(_pathsCache.map(p =>
      _fetch(`${OLLMO_API}/config/paths/${encodeURIComponent(p.name)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target }),
      })
    ));
    fetchPathsForRamTier();
  } catch {}
}

// --- Advanced tab ---
let _advSvcStatus = {};

async function loadAdvancedTab() {
  try {
    const [pathsR, routingR, svcsR, statusR] = await Promise.all([
      _fetch(`${OLLMO_API}/config/paths`),
      _fetch(`${OLLMO_API}/config/routing`),
      _fetch(`${OLLMO_API}/config/services`),
      _fetch(`${OLLMO_API}/system/status`),
    ]);
    const paths    = await pathsR.json();
    const routing  = await routingR.json();
    const services = await svcsR.json();
    const status   = await statusR.json();
    const svcArr   = status.services || [];
    _advSvcStatus = {};
    for (const s of (Array.isArray(svcArr) ? svcArr : [])) _advSvcStatus[s.name] = s.status;

    renderDiskMap(paths);
    renderAdvConnectionMap(routing);
    renderPortMap(services);
    renderVolumeMap();
    loadWorkflowsList();
  } catch (e) {
    console.warn("loadAdvancedTab error:", e);
  }
}

// --- Workflow discovery ---
const _TIER_COLORS = { nvme: "#4fc3f7", sata: "#ffb74d", ram: "#81c784", custom: "#b0bec5" };

async function loadWorkflowsList() {
  const el = document.getElementById("adv-workflows-list");
  if (!el) return;
  try {
    const r = await _fetch(`${OLLMO_API}/workflows`);
    const list = await r.json();
    renderWorkflowsList(el, list);
  } catch (e) {
    el.innerHTML = '<span class="dim">Failed to load workflows</span>';
  }
}

function renderWorkflowsList(el, workflows) {
  if (!workflows.length) {
    el.innerHTML = '<span class="dim">No workflows found. Drop .json files into /mnt/oaio/workflows/</span>';
    return;
  }

  // Group by source
  const groups = {};
  workflows.forEach(w => {
    const key = w.source || "unknown";
    if (!groups[key]) groups[key] = [];
    groups[key].push(w);
  });

  let html = '';
  for (const [source, items] of Object.entries(groups)) {
    const srcLabel = source.replace("/mnt/oaio/", "");
    html += '<div style="margin-bottom:8px">' +
      '<span class="dim" style="font-size:9px;text-transform:uppercase;letter-spacing:1px">' + _esc(srcLabel) + '</span>' +
      '</div>';
    items.forEach(w => {
      const tierColor = _TIER_COLORS[w.tier] || _TIER_COLORS.custom;
      const mod = w.modified ? new Date(w.modified).toLocaleDateString() : "?";
      html += '<div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:' + tierColor + ';flex-shrink:0" title="' + _esc(w.tier) + '"></span>' +
        '<span style="flex:1;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + _esc(w.path) + '">' + _esc(w.name) + '</span>' +
        '<span class="dim" style="font-size:9px;flex-shrink:0">' + _esc(w.tier) + '</span>' +
        '<span class="dim" style="font-size:9px;flex-shrink:0">' + w.size_kb + ' KB</span>' +
        '<span class="dim" style="font-size:9px;flex-shrink:0">' + _esc(mod) + '</span>' +
        '<button class="grp-pill" style="font-size:8px;padding:2px 6px;flex-shrink:0" data-wf-export="' + _esc(w.path) + '">EXPORT</button>' +
        '</div>';
    });
  }
  el.innerHTML = html;

  // Wire export buttons (no inline onclick -- uses data attribute)
  el.querySelectorAll("[data-wf-export]").forEach(function(btn) {
    btn.addEventListener("click", async function() {
      var wfPath = btn.getAttribute("data-wf-export");
      try {
        var r = await _fetch(OLLMO_API + "/workflows/export?path=" + encodeURIComponent(wfPath));
        var data = await r.json();
        var blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
        var url  = URL.createObjectURL(blob);
        var a    = document.createElement("a");
        a.href = url;
        var fname = (data.oaio_export && data.oaio_export.source_file)
          ? data.oaio_export.source_file.split("/").pop()
          : "workflow";
        a.download = fname + ".oaio-export.json";
        a.click();
        URL.revokeObjectURL(url);
        showAlert("info", "Workflow exported with tier metadata.");
      } catch (e) {
        showAlert("error", "Export failed: " + e.message);
      }
    });
  });
}

function renderDiskMap(paths) {
  const el = document.getElementById("disk-map");
  if (!el) return;

  // Group paths by target prefix into disk columns
  const groups = { NVME: [], SATA: [], OS: [], RAM: [] };
  const groupMeta = {
    NVME: { mount: "/mnt/storage",      cls: "nvme" },
    SATA: { mount: "/mnt/windows-sata", cls: "sata" },
    RAM:  { mount: "/dev/shm",          cls: "ram"  },
    OS:   { mount: "/",                 cls: "os", displayName: "OS (NVMe)" },
  };

  paths.forEach(p => {
    const t = p.target || "";
    if (t.startsWith("/mnt/storage"))      groups.NVME.push(p);
    else if (t.startsWith("/mnt/windows-sata")) groups.SATA.push(p);
    else if (t.startsWith("/dev/shm"))     groups.RAM.push(p);
    else                                   groups.OS.push(p);
  });

  el.innerHTML = "";
  el.className = "disk-map";

  // Render columns in fixed order, skip empty groups
  const order = ["SATA", "NVME", "OS", "RAM"];
  order.forEach(name => {
    const items = groups[name];
    if (!items.length) return;
    const meta = groupMeta[name];

    const col = document.createElement("div");
    col.className = `disk-col ${meta.cls}`;

    col.innerHTML =
      `<div class="disk-col-title ${meta.cls}">` +
        `<span class="disk-col-name">${meta.displayName || name}</span>` +
        `<span class="disk-col-mount">${meta.mount}</span>` +
        `<span class="disk-col-count">${items.length}</span>` +
      `</div>`;

    const body = document.createElement("div");
    body.className = "disk-col-body";

    items.forEach(p => {
      const dot = p.exists ? "ok" : "missing";
      const ctrs = (p.containers || []).map(c => `<span class="ctr-tag">${c}</span>`).join("");
      const pill = document.createElement("div");
      pill.className = `disk-pill ${dot}`;
      pill.innerHTML =
        `<span class="disk-dot ${dot}"></span>` +
        `<span class="disk-pill-label">${p.label}</span>` +
        `<span class="disk-pill-target path-tier-${meta.cls}">${p.target || "—"}</span>` +
        (p.link ? `<span class="disk-pill-link path-tier-${meta.cls}">${p.link}</span>` : "") +
        (ctrs ? `<div class="ctr-tags">${ctrs}</div>` : "");
      body.appendChild(pill);
    });
    col.appendChild(body);

    el.appendChild(col);
  });
}

// --- Connection Map (Advanced tab) ---
function renderAdvConnectionMap(routing) {
  const el = document.getElementById("conn-map");
  if (!el) return;

  const connections = [];
  if (routing.tts_url) {
    const to = routing.tts_url.replace(/^https?:\/\//, "").split(":")[0];
    connections.push({ from: "open-webui", to, label: "TTS", url: routing.tts_url });
  }
  if (routing.ollama_url) {
    const to = routing.ollama_url.replace(/^https?:\/\//, "").split(":")[0];
    connections.push({ from: "open-webui", to, label: "LLM", url: routing.ollama_url });
  }
  if (routing.image_gen_url) {
    const to = routing.image_gen_url.replace(/^https?:\/\//, "").split(":")[0];
    connections.push({ from: "open-webui", to, label: "Image Gen", url: routing.image_gen_url });
  }
  if (routing.stt_url) {
    const to = routing.stt_url.replace(/^https?:\/\//, "").split(":")[0];
    connections.push({ from: "open-webui", to, label: "STT", url: routing.stt_url });
  }
  connections.push({ from: "kokoro-tts", to: "rvc", label: "Voice Conv", url: "internal pipeline" });
  connections.push({ from: "f5-tts", to: "rvc", label: "Clone → Conv", url: "internal pipeline" });

  el.innerHTML = connections.map(c => {
    const fromDot = (_advSvcStatus[c.from] === "running") ? "ok" : "err";
    const toDot   = (_advSvcStatus[c.to] === "running") ? "ok" : "err";
    return `<div class="conn-row">` +
      `<span class="conn-node"><span class="svc-dot ${fromDot}" data-adv-svc="${c.from}"></span>${c.from}</span>` +
      `<span class="conn-arrow">→</span>` +
      `<span class="conn-label">${c.label}</span>` +
      `<span class="conn-arrow">→</span>` +
      `<span class="conn-node"><span class="svc-dot ${toDot}" data-adv-svc="${c.to}"></span>${c.to}</span>` +
      `<span class="conn-url">${c.url}</span>` +
    `</div>`;
  }).join("");
}

// --- Port Map ---
function renderPortMap(services) {
  const el = document.getElementById("port-map");
  if (!el) return;

  let html = `<table class="port-table"><thead><tr>` +
    `<th>SERVICE</th><th>PORT</th><th>MODE</th><th>STATUS</th>` +
    `</tr></thead><tbody>`;

  const GROUP_TO_MODE = { oLLM: "oLLMo", oAudio: "oAudio", Render: "comfyui-flex", Control: "Control" };
  for (const [name, svc] of Object.entries(services)) {
    const st = _advSvcStatus[name] || "unknown";
    const dot = st === "running" ? "ok" : st === "exited" ? "err" : "warn";
    const modeLabel = GROUP_TO_MODE[svc.group] || svc.group || "—";
    html += `<tr>` +
      `<td><span class="port-svc"><span class="svc-dot ${dot}" data-adv-svc="${name}"></span>${name}</span></td>` +
      `<td class="port-num">${svc.port || "—"}</td>` +
      `<td><span class="port-group ${svc.group || "Other"}">${modeLabel}</span></td>` +
      `<td><span class="port-status ${st}" data-adv-status="${name}">${st}</span></td>` +
    `</tr>`;
  }
  html += `</tbody></table>`;
  el.innerHTML = html;
}

// --- Volume Map ---
const VOLUME_MAP = {
  ollama:       [{ host: "/mnt/oaio/ollama", mount: "/root/.ollama" }],
  "open-webui": [{ host: "open-webui (named vol)", mount: "/app/backend/data" }],
  "kokoro-tts": [{ host: "/mnt/oaio/kokoro-voices", mount: "/models" }],
  rvc: [
    { host: "/mnt/oaio/audio", mount: "/models" },
    { host: "/mnt/oaio/rvc-ref", mount: "/rvc/audio" },
    { host: "/mnt/oaio/rvc-weights", mount: "/rvc/assets/weights" },
    { host: "/mnt/oaio/rvc-indices", mount: "/rvc/assets/indices" },
  ],
  "f5-tts": [
    { host: "/mnt/oaio/hf-cache", mount: "/hf-cache" },
    { host: "/mnt/oaio/ref-audio", mount: "/ref-audio" },
  ],
  styletts2: [{ host: "/mnt/oaio/hf-cache", mount: "/root/.cache/huggingface" }],
  comfyui: [
    { host: "/mnt/oaio/models", mount: "/ComfyUI/models" },
    { host: "/mnt/oaio/custom-nodes", mount: "/ComfyUI/custom_nodes" },
    { host: "/mnt/oaio/comfyui-user", mount: "/ComfyUI/user" },
    { host: "/mnt/oaio/outputs", mount: "/ComfyUI/output" },
    { host: "/mnt/oaio/inputs", mount: "/ComfyUI/input" },
    { host: "/mnt/oaio/hf-cache", mount: "/hf-cache" },
  ],
};

function _pathTierClass(path) {
  if (path.startsWith("/mnt/windows-sata")) return "path-tier-sata";
  if (path.startsWith("/mnt/storage"))      return "path-tier-nvme";
  if (path.startsWith("/dev/shm"))          return "path-tier-ram";
  if (path.startsWith("/mnt/oaio"))         return "path-tier-nvme";
  return "path-tier-os";
}

function renderVolumeMap() {
  const el = document.getElementById("vol-map");
  if (!el) return;
  el.className = "vol-map";
  el.innerHTML = "";

  for (const [name, mounts] of Object.entries(VOLUME_MAP)) {
    const st = _advSvcStatus[name] || "unknown";
    const dot = st === "running" ? "ok" : st === "exited" ? "err" : "warn";

    const svc = document.createElement("div");
    svc.className = "vol-svc";
    svc.innerHTML =
      `<div class="vol-svc-title">` +
        `<span class="svc-dot ${dot}" data-adv-svc="${name}"></span>` +
        `${name}` +
        `<span class="vol-svc-count">${mounts.length}</span>` +
      `</div>`;

    const body = document.createElement("div");
    body.className = "vol-svc-body";
    mounts.forEach(m => {
      body.innerHTML +=
        `<div class="vol-mount">` +
          `<span class="vol-host ${_pathTierClass(m.host)}">${m.host}</span>` +
          `<span class="vol-arrow">→</span>` +
          `<span class="vol-container">${m.mount}</span>` +
        `</div>`;
    });
    svc.appendChild(body);
    el.appendChild(svc);
  }
}

// --- Live status dot updates for Advanced tab ---
function updateAdvStatusDots(svcArr) {
  const arr = Array.isArray(svcArr) ? svcArr : [];
  let changed = false;
  for (const s of arr) {
    if (_advSvcStatus[s.name] !== s.status) { _advSvcStatus[s.name] = s.status; changed = true; }
  }
  if (!changed) return;
  // Update all data-adv-svc dots
  document.querySelectorAll("[data-adv-svc]").forEach(dot => {
    const name = dot.dataset.advSvc;
    const st = _advSvcStatus[name] || "unknown";
    dot.className = `svc-dot ${st === "running" ? "ok" : st === "exited" ? "err" : "warn"}`;
  });
  // Update port status text
  document.querySelectorAll("[data-adv-status]").forEach(el => {
    const name = el.dataset.advStatus;
    const st = _advSvcStatus[name] || "unknown";
    el.textContent = st;
    el.className = `port-status ${st}`;
  });
}

// --- Wire Pulse on API Calls ---
const PATH_TO_SERVICE = {
  "/v1/chat/completions": "ollama",
  "/api/chat": "ollama",
  "/api/generate": "ollama",
  "/v1/audio/speech": "rvc",
  "/services/ollama": "ollama",
  "/services/comfyui": "comfyui",
  "/services/rvc": "rvc",
  "/services/kokoro-tts": "kokoro-tts",
  "/services/f5-tts": "f5-tts",
  "/services/styletts2": "styletts2",
  "/services/open-webui": "open-webui",
};

function matchPathToService(path) {
  for (const [prefix, svc] of Object.entries(PATH_TO_SERVICE)) {
    if (path.startsWith(prefix)) return svc;
  }
  return null;
}

function pulseWire(fromSvc, toSvc) {
  if (!graph || !canvas) return;
  const fromNode = _findNode(fromSvc);
  const toNode   = _findNode(toSvc);
  if (!fromNode || !toNode) return;

  // Find link between them
  const link = Object.values(graph.links || {}).find(l =>
    l && graph.getNodeById(l.origin_id) === fromNode &&
         graph.getNodeById(l.target_id) === toNode
  );
  if (!link) return;

  // Get positions in canvas coordinates
  const ds = canvas.ds;
  if (!ds) return;

  // Output slot position (right side of source node)
  const fromX = fromNode.pos[0] + (fromNode.size?.[0] || 180);
  const fromY = fromNode.pos[1] + 30 + link.origin_slot * 20;
  // Input slot position (left side of target node)
  const toX = toNode.pos[0];
  const toY = toNode.pos[1] + 30 + link.target_slot * 20;

  // Convert to screen coordinates: screenPos = (canvasPos + offset) * scale
  const sx1 = (fromX + ds.offset[0]) * ds.scale;
  const sy1 = (fromY + ds.offset[1]) * ds.scale;
  const sx2 = (toX + ds.offset[0]) * ds.scale;
  const sy2 = (toY + ds.offset[1]) * ds.scale;

  // Create pulse dot
  const wrap = document.getElementById("canvas-wrap");
  if (!wrap) return;
  const dot = document.createElement("div");
  dot.className = "wire-pulse";
  dot.style.left = sx1 + "px";
  dot.style.top  = sy1 + "px";
  wrap.appendChild(dot);

  // Animate from source to target
  const duration = 800;
  const start = performance.now();
  function animate(now) {
    const t = Math.min((now - start) / duration, 1);
    const ease = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
    dot.style.left = (sx1 + (sx2 - sx1) * ease) + "px";
    dot.style.top  = (sy1 + (sy2 - sy1) * ease) + "px";
    dot.style.opacity = t > 0.7 ? 1 - (t - 0.7) / 0.3 : 1;
    if (t < 1) {
      requestAnimationFrame(animate);
    } else {
      dot.remove();
    }
  }
  requestAnimationFrame(animate);
}

// --- Live Monitor (LIVE tab compact card) ---
let _liveMonitorWs = null;
let _liveMonitorPollTimer = null;
let _liveTabActive = true;

async function startLiveMonitor() {
  // Stats polling
  if (!_liveMonitorPollTimer) {
    fetchLiveMonitorStats();
    _liveMonitorPollTimer = setInterval(fetchLiveMonitorStats, 3000);
  }
  // WS stream — probe backend first to avoid Firefox console noise
  if (!_liveMonitorWs || _liveMonitorWs.readyState > 1) {
    // Clean up stale WS to prevent handler leaks
    if (_liveMonitorWs) { _liveMonitorWs.onclose = null; _liveMonitorWs.onerror = null; _liveMonitorWs.onmessage = null; }
    try {
      const probe = await _fetch(`${OLLMO_API}/api/monitor/stats`).catch(() => null);
      if (!probe || !probe.ok) { setTimeout(startLiveMonitor, 3000); return; }
      _liveMonitorWs = new WebSocket(_wsUrl('/api/monitor/ws'));
      _liveMonitorWs.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          appendLiveMonitorRow(data);
          const svc = matchPathToService(data.path);
          if (svc && graph) {
            // Find which service is calling this one by checking routing wires
            const targetNode = (graph._nodes || []).find(n => n._svc?.name === svc);
            if (targetNode) {
              // Pulse from any connected input node to this service
              const links = Object.values(graph.links || {});
              links.forEach(l => {
                if (l && graph.getNodeById(l.target_id) === targetNode) {
                  const srcNode = graph.getNodeById(l.origin_id);
                  if (srcNode?._svc) pulseWire(srcNode._svc.name, svc);
                }
              });
            }
          }
        } catch {}
      };
      _liveMonitorWs.onclose = () => {
        if (_liveTabActive) setTimeout(startLiveMonitor, 3000);
      };
      _liveMonitorWs.onerror = () => { if (_liveMonitorWs) _liveMonitorWs.close(); };
    } catch (e) {
      console.warn("Live monitor WS connect failed:", e.message);
      if (_liveTabActive) setTimeout(startLiveMonitor, 3000);
    }
  }
}

function stopLiveMonitor() {
  if (_liveMonitorPollTimer) { clearInterval(_liveMonitorPollTimer); _liveMonitorPollTimer = null; }
  if (_liveMonitorWs) { _liveMonitorWs.onclose = null; _liveMonitorWs.close(); _liveMonitorWs = null; }
}

// ── Debugger logs card (LIVE tab) ──────────────────────────────────────────
let _dbgWs = null;
let _dbgContainer = null;
let _dbgFilter = "all"; // "all" | "errors"
const _DBG_MAX_LINES = 500;

function initDebuggerCard() {
  // Container selector buttons
  document.querySelectorAll(".dbg-ctr-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const ctr = btn.dataset.ctr;
      if (_dbgContainer === ctr) {
        // Deselect — disconnect
        debuggerDisconnect();
        btn.classList.remove("active");
        _dbgContainer = null;
      } else {
        // Select new container
        document.querySelectorAll(".dbg-ctr-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        debuggerSelectContainer(ctr);
      }
    });
  });

  // Filter toggle buttons
  document.querySelectorAll(".dbg-filter-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".dbg-filter-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      _dbgFilter = btn.dataset.filter;
      // Reconnect with new filter if a container is active
      if (_dbgContainer) {
        debuggerSelectContainer(_dbgContainer);
      }
    });
  });
}

function debuggerSelectContainer(name) {
  debuggerDisconnect();
  _dbgContainer = name;
  const output = document.getElementById("dbg-output");
  if (!output) return;
  output.innerHTML = '<span class="dim">Connecting to ' + _esc(name) + '...</span>';

  if (_dbgFilter === "errors") {
    // Fetch errors via REST, then connect WS for live stream (filtered client-side)
    _fetch(`${OLLMO_API}/extensions/debugger/errors/${encodeURIComponent(name)}?lines=200`)
      .then(r => r.json())
      .then(data => {
        output.innerHTML = "";
        if (data.error) {
          output.innerHTML = '<span class="dbg-line level-error">' + _esc(data.error) + '</span>';
          return;
        }
        (data.lines || []).forEach(line => {
          _dbgAppendLine(output, line, "error");
        });
        _dbgScrollBottom(output);
        // Now connect WS for live updates, filtering client-side
        _dbgConnectWs(name, true);
      })
      .catch(e => {
        output.innerHTML = '<span class="dbg-line level-error">' + _esc("Fetch failed: " + e.message) + '</span>';
      });
  } else {
    // Fetch recent logs via REST, then connect WS
    _fetch(`${OLLMO_API}/extensions/debugger/logs/${encodeURIComponent(name)}?lines=100`)
      .then(r => r.json())
      .then(data => {
        output.innerHTML = "";
        if (data.error) {
          output.innerHTML = '<span class="dbg-line level-error">' + _esc(data.error) + '</span>';
          return;
        }
        (data.lines || []).forEach(line => {
          // Detect level client-side for initial batch (REST returns plain strings)
          let level = "info";
          if (/error|exception|traceback|critical/i.test(line)) level = "error";
          else if (/warn|warning/i.test(line)) level = "warn";
          _dbgAppendLine(output, line, level);
        });
        _dbgScrollBottom(output);
        _dbgConnectWs(name, false);
      })
      .catch(e => {
        output.innerHTML = '<span class="dbg-line level-error">' + _esc("Fetch failed: " + e.message) + '</span>';
      });
  }
}

function _dbgConnectWs(container, errorsOnly) {
  try {
    _dbgWs = new WebSocket(_wsUrl(`/extensions/debugger/ws/${encodeURIComponent(container)}`));
  } catch (e) {
    console.warn("Debugger WS connect failed:", e.message);
    return;
  }
  _dbgWs.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      // If errors-only filter is active, skip non-error/warn lines
      if (errorsOnly && data.level === "info") return;
      const output = document.getElementById("dbg-output");
      if (!output) return;
      _dbgAppendLine(output, data.line, data.level);
      _dbgScrollBottom(output);
    } catch {}
  };
  _dbgWs.onclose = () => {
    // Reconnect if still viewing this container
    if (_dbgContainer === container) {
      setTimeout(() => {
        if (_dbgContainer === container) _dbgConnectWs(container, errorsOnly);
      }, 3000);
    }
  };
  _dbgWs.onerror = () => { if (_dbgWs) _dbgWs.close(); };
}

function _dbgAppendLine(output, text, level) {
  const div = document.createElement("div");
  div.className = "dbg-line level-" + (level || "info");
  div.textContent = text; // textContent is safe — no innerHTML injection
  output.appendChild(div);
  // Trim old lines
  while (output.children.length > _DBG_MAX_LINES) {
    output.removeChild(output.firstChild);
  }
}

function _dbgScrollBottom(el) {
  // Auto-scroll only if user is near bottom (within 40px)
  const nearBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 40;
  if (nearBottom) {
    requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
  }
}

function debuggerDisconnect() {
  if (_dbgWs) {
    _dbgWs.onclose = null;
    _dbgWs.onerror = null;
    _dbgWs.onmessage = null;
    _dbgWs.close();
    _dbgWs = null;
  }
}

async function fetchLiveMonitorStats() {
  try {
    const r = await _fetch(`${OLLMO_API}/api/monitor/stats`);
    const s = await r.json();
    const el = (id) => document.getElementById(id);
    el("lms-total").textContent = s.total_reqs;
    el("lms-latency").textContent = `${s.avg_latency_ms} ms`;
    el("lms-errors").textContent = s.error_count;
  } catch {}
}

function appendLiveMonitorRow(entry) {
  const stream = document.getElementById("live-monitor-stream");
  if (!stream) return;
  const row = document.createElement("div");
  row.className = "api-req-row";
  const mc = entry.method.toLowerCase();
  const sc = entry.status >= 500 ? "s5xx" : entry.status >= 400 ? "s4xx" : "s2xx";
  row.innerHTML = `
    <span class="api-req-ts">${entry.ts}</span>
    <span class="api-req-method api-ep-method ${mc}">${entry.method}</span>
    <span class="api-req-path">${entry.path}</span>
    <span class="api-req-status ${sc}">${entry.status || "—"}</span>
    <span class="api-req-latency">${entry.latency_ms}ms</span>
  `;
  stream.appendChild(row);
  while (stream.children.length > 15) stream.removeChild(stream.firstChild);
  if (!stream.matches(":hover")) stream.scrollTop = stream.scrollHeight;
}

// --- API tab ---
let _openApiCache = null;
let _monitorWs = null;
let _monitorPollTimer = null;
let _topoPollTimer = null;
let _apiTabActive = false;

function loadApiTab() {
  _apiTabActive = true;
  renderApiGuide();
  fetchTopology();
  if (!_topoPollTimer) _topoPollTimer = setInterval(fetchTopology, 5000);
  if (!_openApiCache) fetchOpenApiSpec();
  startMonitor();
}

function unloadApiTab() {
  _apiTabActive = false;
  stopMonitor();
  if (_topoPollTimer) { clearInterval(_topoPollTimer); _topoPollTimer = null; }
}

function renderApiGuide() {
  const body = document.getElementById("api-guide-body");
  if (!body || body.children.length > 0) return;
  body.innerHTML = `
    <details class="api-guide-section" open>
      <summary>MODES</summary>
      <div class="api-guide-content">
        <p>Modes define <b>workload profiles</b> — each specifies which services run and how VRAM is allocated.</p>
        <ul>
          <li><code>POST /modes/{name}/activate</code> — starts mode services, stops others</li>
          <li><code>POST /modes/{name}/deactivate</code> — removes from active tracking</li>
          <li><code>GET /modes/{name}/check</code> — pre-flight VRAM projection (dry run)</li>
          <li>Multiple modes can be active simultaneously (primary + secondary)</li>
          <li>Each mode has a <code>vram_budget_gb</code> ceiling and per-service allocations</li>
        </ul>
      </div>
    </details>
    <details class="api-guide-section">
      <summary>SERVICES &amp; NODES</summary>
      <div class="api-guide-content">
        <p>Each service = 1 Docker container, defined in <code>services.json</code>. The CONFIG tab visualizes these as LiteGraph nodes.</p>
        <ul>
          <li><code>POST /services/{name}/start|stop</code> — container lifecycle</li>
          <li><code>PATCH /config/services/{name}</code> — update priority, limit_mode, auto_restore</li>
          <li>Priority: 1 = protected ... 5 = first killed under OOM</li>
          <li>Nodes connect via routing config (TTS URL, Ollama URL, etc.)</li>
          <li>Sub-nodes: Ollama has models, RVC has voice models, ComfyUI has workflows, Kokoro has voices</li>
        </ul>
      </div>
    </details>
    <details class="api-guide-section">
      <summary>CONNECTIONS &amp; ROUTING</summary>
      <div class="api-guide-content">
        <p>Services connect to each other through <b>routing config</b> — URL-based service discovery within the Docker network.</p>
        <ul>
          <li><code>GET /config/routing</code> — current routing table</li>
          <li><code>POST /config/routing</code> — update route URLs</li>
          <li>Open WebUI → Ollama (LLM inference) via <code>ollama_url</code></li>
          <li>Open WebUI → RVC proxy (TTS+voice conversion) via <code>tts_url</code></li>
          <li>Open WebUI → ComfyUI (image generation) via <code>image_gen_url</code></li>
          <li>Audio chain: text → Kokoro TTS → RVC → output (or F5-TTS for cloning)</li>
        </ul>
      </div>
    </details>
    <details class="api-guide-section">
      <summary>ENFORCEMENT</summary>
      <div class="api-guide-content">
        <p>OOM enforcer polls VRAM every 5s. Above hard threshold → kills lowest-priority hard-limit service.</p>
        <ul>
          <li><code>POST /enforcement/enable|disable</code> — master toggle</li>
          <li><code>limit_mode</code>: <code>soft</code> = warn, <code>hard</code> = auto-kill, <code>off</code> = exempt</li>
          <li><code>POST /emergency/kill</code> — stops ALL containers immediately</li>
        </ul>
      </div>
    </details>
    <details class="api-guide-section">
      <summary>AUDIO PIPELINE (port 8002)</summary>
      <div class="api-guide-content">
        <ul>
          <li><code>POST /speak</code> — Kokoro TTS → optional RVC voice conversion</li>
          <li><code>POST /clone</code> — F5-TTS voice cloning (ref audio + transcription)</li>
          <li><code>POST /convert</code> — RVC voice conversion (audio in → audio out)</li>
        </ul>
        <p>Voices: alloy/nova → af_heart, shimmer/echo → af_sky, fable → bf_emma, onyx → am_adam</p>
      </div>
    </details>
    <details class="api-guide-section">
      <summary>EXTENSIONS</summary>
      <div class="api-guide-content">
        <p>Live in <code>extensions/&lt;name&gt;/</code> with <code>manifest.json</code>. Hot-reload via volume mount.</p>
        <ul>
          <li><code>GET /extensions</code> — list all with enabled state</li>
          <li><code>POST /extensions/{name}/enable|disable</code></li>
          <li>Built-in: <b>Fleet</b> (multi-node orchestration), <b>Debugger</b> (container logs)</li>
        </ul>
      </div>
    </details>
  `;
}

// ── Live Topology ──────────────────────────────────────────────────────────────
async function fetchTopology() {
  const body = document.getElementById("api-topology-body");
  if (!body) return;
  try {
    const [modesR, svcsR, routingR, statusR] = await Promise.all([
      _fetch(`${OLLMO_API}/modes`),
      _fetch(`${OLLMO_API}/config/services`),
      _fetch(`${OLLMO_API}/config/routing`),
      _fetch(`${OLLMO_API}/system/status`),
    ]);
    const modes    = await modesR.json();
    const services = await svcsR.json();
    const routing  = await routingR.json();
    const status   = await statusR.json();
    renderTopology(body, modes, services, routing, status);
  } catch (e) {
    body.innerHTML = `<span class="dim">Failed to load topology: ${_esc(e.message)}</span>`;
  }
}

function renderTopology(body, modes, services, routing, status) {
  const activeModes = status.active_modes || [];
  // services from /system/status is an array [{name, status}, ...] — convert to lookup
  const svcStatusRaw = status.services || [];
  const svcStatus = {};
  if (Array.isArray(svcStatusRaw)) {
    for (const s of svcStatusRaw) svcStatus[s.name] = s;
  } else {
    Object.assign(svcStatus, svcStatusRaw);
  }

  // ── MODES SECTION ──
  let modesHtml = `<div class="topo-section"><div class="topo-section-title">MODES</div><div class="topo-modes">`;
  for (const [key, mode] of Object.entries(modes)) {
    const isActive = activeModes.includes(key);
    const activeClass = isActive ? " topo-active" : "";
    const svcList = (mode.services || []).map(s => {
      const svc = services[s];
      const st = svcStatus[s]?.status || "unknown";
      const dot = st === "running" ? "ok" : st === "exited" ? "err" : "warn";
      const vram = mode.allocations?.[s] || 0;
      return `<span class="topo-svc-chip"><span class="svc-dot ${dot}"></span>${_esc(s)}${vram ? ` <span class="topo-alloc">${_esc(vram)}G</span>` : ""}</span>`;
    }).join("");
    const eKey = _esc(key);
    modesHtml += `
      <div class="topo-mode-card${activeClass}" data-mode="${eKey}">
        <div class="topo-mode-header">
          <span class="topo-mode-name">${_esc(mode.name || key)}</span>
          ${isActive ? '<span class="topo-badge active">ACTIVE</span>' : '<span class="topo-badge">inactive</span>'}
          <span class="topo-budget">${_esc(mode.vram_budget_gb || 0)} GB budget</span>
        </div>
        ${mode.description ? `<div class="topo-mode-desc">${_esc(mode.description)}</div>` : ""}
        <div class="topo-mode-services">${svcList || '<span class="dim">no services</span>'}</div>
        <div class="topo-mode-api">
          <code><span class="topo-method post">POST</span> /modes/${eKey}/activate</code>
          <code><span class="topo-method post">POST</span> /modes/${eKey}/deactivate</code>
          <code><span class="topo-method get">GET</span> /modes/${eKey}/check</code>
        </div>
      </div>`;
  }
  modesHtml += `</div></div>`;

  // ── NODES SECTION ──
  let nodesHtml = `<div class="topo-section"><div class="topo-section-title">NODES (SERVICES)</div><div class="topo-nodes">`;
  const sortedNodes = Object.entries(services).sort((a, b) => {
    const aRun = (svcStatus[a[0]]?.status === "running") ? 0 : 1;
    const bRun = (svcStatus[b[0]]?.status === "running") ? 0 : 1;
    return aRun - bRun;
  });
  for (const [name, svc] of sortedNodes) {
    const st = svcStatus[name]?.status || "unknown";
    const dot = st === "running" ? "ok" : st === "exited" ? "err" : "warn";
    const inModes = Object.entries(modes)
      .filter(([_, m]) => (m.services || []).includes(name))
      .map(([k, m]) => ({ key: k, label: m.name || k }));

    const grpCls = { oLLM: "grp-llm", oAudio: "grp-audio", Render: "grp-render", Control: "grp-control" }[svc.group] || "";
    const grpLabel = { oLLM: "oLLMo", oAudio: "oAudio", Render: "Render", Control: "Control" }[svc.group] || svc.group || "";
    const caps = (svc.capabilities || []).map(c => typeof c === "string" ? c : (c.label || c.type || "")).filter(Boolean);
    const eName = _esc(name);
    nodesHtml += `
      <div class="topo-node-card">
        <div class="topo-node-header">
          <span class="svc-dot ${dot}"></span>
          <span class="topo-node-name">${eName}</span>
          <span class="topo-node-group ${grpCls}">${_esc(grpLabel)}</span>
          <span class="topo-node-port">:${_esc(svc.port || "—")}</span>
        </div>
        <div class="topo-node-props">
          ${svc.vram_est_gb ? `<span class="topo-prop topo-vram">VRAM ${_esc(svc.vram_est_gb)}G</span>` : ""}
          ${svc.ram_est_gb ? `<span class="topo-prop topo-ram">RAM ${_esc(svc.ram_est_gb)}G</span>` : ""}
          <span class="topo-prop">P${_esc(svc.priority || 3)}</span>
          <span class="topo-prop lm-${_esc(svc.limit_mode || "soft")}">${_esc(svc.limit_mode || "soft")}</span>
          ${svc.auto_restore ? '<span class="topo-prop topo-restore">auto-restore</span>' : ""}
        </div>
        ${inModes.length ? `<div class="topo-node-modes">In: ${inModes.map(m => `<span class="topo-mode-ref" data-mode="${_esc(m.key)}">${_esc(m.label)}</span>`).join(" ")}</div>` : ""}
        ${caps.length ? `<div class="topo-node-caps">${caps.map(c => `<span class="topo-cap">${_esc(c)}</span>`).join("")}</div>` : ""}
        <div class="topo-node-api">
          <code><span class="topo-method post">POST</span> /services/${eName}/start</code>
          <code><span class="topo-method post">POST</span> /services/${eName}/stop</code>
        </div>
      </div>`;
  }
  nodesHtml += `</div></div>`;

  // ── CONNECTIONS SECTION ──
  // Parse routing into human-readable connections
  const connections = [];
  if (routing.tts_url) {
    const target = routing.tts_url.replace(/^https?:\/\//, "").split(":")[0];
    connections.push({ from: "open-webui", to: target, label: "TTS", url: routing.tts_url });
  }
  if (routing.ollama_url) {
    const target = routing.ollama_url.replace(/^https?:\/\//, "").split(":")[0];
    connections.push({ from: "open-webui", to: target, label: "LLM", url: routing.ollama_url });
  }
  if (routing.image_gen_url) {
    const target = routing.image_gen_url.replace(/^https?:\/\//, "").split(":")[0];
    connections.push({ from: "open-webui", to: target, label: "Image Gen", url: routing.image_gen_url });
  }
  // Audio chain connections (implicit from architecture)
  connections.push({ from: "kokoro-tts", to: "rvc", label: "Voice Convert", url: "internal pipeline" });
  connections.push({ from: "f5-tts", to: "rvc", label: "Clone → Convert", url: "internal pipeline" });

  let connHtml = `<div class="topo-section"><div class="topo-section-title">CONNECTIONS</div><div class="topo-connections">`;
  for (const c of connections) {
    const fromSt = svcStatus[c.from]?.status === "running" ? "ok" : "err";
    const toSt   = svcStatus[c.to]?.status === "running" ? "ok" : "err";
    connHtml += `
      <div class="topo-conn-row">
        <span class="topo-conn-node"><span class="svc-dot ${fromSt}"></span>${c.from}</span>
        <span class="topo-conn-arrow">→</span>
        <span class="topo-conn-label">${c.label}</span>
        <span class="topo-conn-arrow">→</span>
        <span class="topo-conn-node"><span class="svc-dot ${toSt}"></span>${c.to}</span>
        <code class="topo-conn-url">${c.url}</code>
      </div>`;
  }
  connHtml += `</div>`;
  connHtml += `<div class="topo-routing-api"><b>Routing API:</b> <code><span class="topo-method get">GET</span> /config/routing</code> · <code><span class="topo-method post">POST</span> /config/routing</code></div>`;
  connHtml += `</div>`;

  body.innerHTML = modesHtml + nodesHtml + connHtml;
}

async function fetchOpenApiSpec() {
  try {
    const r = await _fetch(`${OLLMO_API}/openapi.json`);
    _openApiCache = await r.json();
    renderEndpoints(_openApiCache);
  } catch (e) {
    document.getElementById("api-endpoints-body").innerHTML = `<span class="dim">Failed to load OpenAPI spec</span>`;
  }
}

function renderEndpoints(spec) {
  const body = document.getElementById("api-endpoints-body");
  const filters = document.getElementById("api-tag-filters");
  if (!body || !spec.paths) return;

  // Group by tag
  const groups = {};
  for (const [path, methods] of Object.entries(spec.paths)) {
    for (const [method, info] of Object.entries(methods)) {
      if (method === "options" || method === "head") continue;
      const tag = (info.tags && info.tags[0]) || "Other";
      if (!groups[tag]) groups[tag] = [];
      groups[tag].push({ method: method.toUpperCase(), path, summary: info.summary || "", info });
    }
  }

  // Add WebSocket entries manually
  if (!groups["System"]) groups["System"] = [];
  groups["System"].unshift({ method: "WS", path: "/ws", summary: "1Hz status push", info: {} });
  if (!groups["Monitor"]) groups["Monitor"] = [];
  groups["Monitor"].unshift({ method: "WS", path: "/api/monitor/ws", summary: "Live request stream", info: {} });

  // Render filter pills
  const tags = Object.keys(groups).sort();
  filters.innerHTML = `<button class="api-tag-pill active" data-tag="all">ALL</button>` +
    tags.map(t => `<button class="api-tag-pill" data-tag="${t}">${t}</button>`).join("");

  filters.querySelectorAll(".api-tag-pill").forEach(pill => {
    pill.addEventListener("click", () => {
      filters.querySelectorAll(".api-tag-pill").forEach(p => p.classList.remove("active"));
      pill.classList.add("active");
      const tag = pill.dataset.tag;
      body.querySelectorAll(".api-ep-group").forEach(g => {
        g.style.display = (tag === "all" || g.dataset.tag === tag) ? "" : "none";
      });
    });
  });

  // Render endpoint groups
  let html = "";
  for (const tag of tags) {
    html += `<div class="api-ep-group" data-tag="${tag}">`;
    html += `<div class="api-ep-group-title">${tag}</div>`;
    for (const ep of groups[tag]) {
      const mc = ep.method.toLowerCase();
      const id = `ep-${mc}-${ep.path.replace(/[^a-z0-9]/gi, "_")}`;
      html += `<div class="api-ep-row" data-detail="${id}">`;
      html += `<span class="api-ep-method ${mc}">${ep.method}</span>`;
      html += `<span class="api-ep-path">${ep.path}<span class="api-ep-summary">${ep.summary}</span></span>`;
      html += `</div>`;
      html += `<div class="api-ep-detail" id="${id}">${renderEpDetail(ep)}</div>`;
    }
    html += `</div>`;
  }
  body.innerHTML = html;

  // Click to expand
  body.querySelectorAll(".api-ep-row").forEach(row => {
    row.addEventListener("click", () => {
      const detail = document.getElementById(row.dataset.detail);
      if (detail) detail.classList.toggle("open");
    });
  });
}

function renderEpDetail(ep) {
  const info = ep.info;
  let html = "";
  if (info.parameters && info.parameters.length) {
    html += `<b>Parameters:</b><br>`;
    for (const p of info.parameters) {
      html += `<code>${p.name}</code> (${p.in}) — ${p.schema?.type || "any"}${p.required ? " <b>required</b>" : ""}<br>`;
    }
  }
  if (info.requestBody) {
    html += `<b>Request Body:</b><br>`;
    const ct = info.requestBody.content;
    const schema = ct?.["application/json"]?.schema;
    if (schema) {
      html += `<pre>${JSON.stringify(schema, null, 2)}</pre>`;
    }
  }
  if (info.responses) {
    const codes = Object.keys(info.responses);
    html += `<b>Responses:</b> ${codes.join(", ")}<br>`;
    for (const code of codes) {
      const resp = info.responses[code];
      const rs = resp.content?.["application/json"]?.schema;
      if (rs) {
        html += `<pre>${code}: ${JSON.stringify(rs, null, 2)}</pre>`;
      }
    }
  }
  if (!html) html = `<span class="dim">No additional details</span>`;
  return html;
}

// Monitor
async function startMonitor() {
  // Stats polling
  if (!_monitorPollTimer) {
    fetchMonitorStats();
    _monitorPollTimer = setInterval(fetchMonitorStats, 2000);
  }
  // WS stream — probe backend first to avoid Firefox console noise
  if (!_monitorWs || _monitorWs.readyState > 1) {
    // Clean up stale WS to prevent handler leaks
    if (_monitorWs) { _monitorWs.onclose = null; _monitorWs.onerror = null; _monitorWs.onmessage = null; }
    try {
      const probe = await _fetch(`${OLLMO_API}/api/monitor/stats`).catch(() => null);
      if (!probe || !probe.ok) { setTimeout(startMonitor, 3000); return; }
      _monitorWs = new WebSocket(_wsUrl('/api/monitor/ws'));
      _monitorWs.onmessage = (e) => {
        try {
          appendMonitorRow(JSON.parse(e.data));
        } catch {}
      };
      _monitorWs.onclose = () => {
        if (_apiTabActive) setTimeout(startMonitor, 3000);
      };
      _monitorWs.onerror = () => { if (_monitorWs) _monitorWs.close(); };
    } catch (e) {
      console.warn("Monitor WS connect failed:", e.message);
      if (_apiTabActive) setTimeout(startMonitor, 3000);
    }
  }
}

function stopMonitor() {
  if (_monitorPollTimer) { clearInterval(_monitorPollTimer); _monitorPollTimer = null; }
  if (_monitorWs) { _monitorWs.onclose = null; _monitorWs.close(); _monitorWs = null; }
}

async function fetchMonitorStats() {
  try {
    const r = await _fetch(`${OLLMO_API}/api/monitor/stats`);
    const s = await r.json();
    document.getElementById("ams-total").textContent = s.total_reqs;
    document.getElementById("ams-latency").textContent = `${s.avg_latency_ms} ms`;
    document.getElementById("ams-errors").textContent = s.error_count;
    const top = s.top_endpoints?.[0];
    document.getElementById("ams-top").textContent = top ? `${top.path} (${top.count})` : "—";
  } catch {}
}

function appendMonitorRow(entry) {
  const stream = document.getElementById("api-monitor-stream");
  if (!stream) return;

  // Filter
  const filter = document.getElementById("api-monitor-filter")?.value?.toLowerCase() || "";
  if (filter && !entry.path.toLowerCase().includes(filter)) return;

  const row = document.createElement("div");
  row.className = "api-req-row";

  const mc = entry.method.toLowerCase();
  const sc = entry.status >= 500 ? "s5xx" : entry.status >= 400 ? "s4xx" : "s2xx";
  row.innerHTML = `
    <span class="api-req-ts">${entry.ts}</span>
    <span class="api-req-method api-ep-method ${mc}">${entry.method}</span>
    <span class="api-req-path">${entry.path}</span>
    <span class="api-req-status ${sc}">${entry.status || "—"}</span>
    <span class="api-req-latency">${entry.latency_ms}ms</span>
  `;
  stream.appendChild(row);

  // Cap at 200 rows
  while (stream.children.length > 200) stream.removeChild(stream.firstChild);

  // Auto-scroll (unless user is hovering to read)
  if (!stream.matches(":hover")) {
    stream.scrollTop = stream.scrollHeight;
  }
}

document.getElementById("api-refresh-btn")?.addEventListener("click", () => {
  _openApiCache = null;
  document.getElementById("api-endpoints-body").innerHTML = `<span class="dim">Loading OpenAPI spec...</span>`;
  fetchOpenApiSpec();
});

// --- Alert banner ---
let alertTimer = null;

function showAlert(level, message) {
  const el = document.getElementById("alert-banner");
  el.textContent = message;
  el.className = "alert-banner " + level;
  el.classList.remove("hidden");
  clearTimeout(alertTimer);
  alertTimer = setTimeout(() => el.classList.add("hidden"), 8000);
}

function syncAlerts(alerts) {
  if (!alerts || alerts.length === 0) return;
  const top = alerts.find(a => a.level === "critical") || alerts[0];
  showAlert(top.level, top.message);
}

// --- Status update (WS + poll) ---
function applyStatusUpdate(d) {
  try {
    setGauge("vram-bar", "vram-label", d.vram?.used_gb || 0, d.vram?.total_gb || 20);
    setGauge("ram-bar",  "ram-label",  d.ram?.used_gb  || 0, d.ram?.total_gb  || 62);

    const gpuPct = (d.gpu?.gpu_use_percent || 0) / 100;
    const gpuBar = document.getElementById("gpu-bar");
    if (gpuBar) {
      gpuBar.style.width = (gpuPct * 100) + "%";
      gpuBar.className = "fill" + (gpuPct > 0.85 ? " hot" : gpuPct > 0.6 ? " warn" : "");
    }
    const gpuLabel = document.getElementById("gpu-label");
    if (gpuLabel) gpuLabel.textContent = `${d.gpu?.gpu_use_percent || 0}%`;

    if (window._lastStorageStats) d.storage = window._lastStorageStats;
    syncAlerts(d.alerts);
    // Live tab cards
    if (d.accounting)             renderAccounting(d.accounting);
    if (d.kill_log !== undefined) renderKillLog(d.kill_log);
    if (d.enforcement)            renderEnforcement(d.enforcement);
    if (d.services)               renderServices(d.services, d.enforcement?.kill_order);
    if (d.ram_tier !== undefined) renderRamTier(d.ram_tier);
    if (d.services) updateAdvStatusDots(d.services);
    if (d.services) syncNodeStatuses(d.services, d.vram);
    renderBenchmark(d.ollama_loaded || [], d.vram, d.gpu, d.ram);

    // Sync active modes from WS
    if (d.active_modes) {
      const changed = JSON.stringify(activeModes) !== JSON.stringify(d.active_modes);
      activeModes = d.active_modes;
      if (changed) {
        renderModeTabs();
        renderModeGrid();
        drawGroupBoxes();
      }
    }
  } catch (e) {
    console.error("applyStatusUpdate error:", e);
  }
}

// --- WebSocket ---
let _statusWs = null;
function connectStatusWS() {
  if (_statusWs) { _statusWs.onclose = null; _statusWs.close(); }
  _statusWs = new WebSocket(_wsUrl('/ws'));
  _statusWs.onopen  = () => {
    const b = document.getElementById('ws-status-banner');
    if (b) b.classList.add('hidden');
  };
  _statusWs.onmessage = event => {
    try {
      const d = JSON.parse(event.data);
      applyStatusUpdate(d);
    } catch(e) { /* ignore parse errors */ }
  };
  _statusWs.onclose = () => {
    _statusWs = null;
    const b = document.getElementById('ws-status-banner');
    if (b) b.classList.remove('hidden');
    setTimeout(connectStatusWS, 3000);
  };
  _statusWs.onerror = () => { _statusWs.close(); };
}

async function pollStorage() {
  try {
    const r = await _fetch(`${OLLMO_API}/config/storage/stats`);
    if (r.ok) { window._lastStorageStats = await r.json(); }
  } catch {}
}

async function poll() {
  try {
    const r = await _fetch(`${OLLMO_API}/system/status`);
    const d = await r.json();
    applyStatusUpdate(d);
  } catch (e) {
    console.warn("oLLMo API not reachable:", e.message);
  }
}

// --- Templates ---
document.getElementById("save-template").addEventListener("click", async () => {
  const name = prompt("Template name:");
  if (!name) return;
  await _fetch(`${OLLMO_API}/templates/save?name=${encodeURIComponent(name)}`, { method: "POST" });
  loadTemplates();
});

async function loadTemplates() {
  try {
    const r    = await _fetch(`${OLLMO_API}/templates`);
    const list = await r.json();
    const el   = document.getElementById("template-list");
    el.innerHTML = "";
    list.forEach(name => {
      const btn     = document.createElement("button");
      btn.className = "mode-btn";
      btn.textContent = name;
      btn.onclick   = () =>
        _fetch(`${OLLMO_API}/templates/${encodeURIComponent(name)}/load`, { method: "POST" })
          .then(poll);
      el.appendChild(btn);
    });
  } catch {}
}

// --- Fleet Tab ---
let _fleetSelectedNode = null;
let _fleetWs = null;

function initFleetTab() {
  document.getElementById("fleet-scan-btn").addEventListener("click", fleetScan);
  document.getElementById("fleet-settings-btn").addEventListener("click", () => {
    document.getElementById("card-fleet-settings").classList.toggle("hidden");
  });
  document.getElementById("fleet-save-settings").addEventListener("click", fleetSaveSettings);
  document.getElementById("fleet-remote-ping").addEventListener("click", fleetPingSelected);
  document.getElementById("fleet-remote-remove").addEventListener("click", fleetRemoveSelected);
  fleetConnectWs();
  fleetLoadSettings();
}

function fleetConnectWs() {
  if (_fleetWs) { _fleetWs.onclose = null; _fleetWs.close(); }
  _fleetWs = new WebSocket(_wsUrl('/extensions/fleet/ws'));
  _fleetWs.onmessage = (e) => {
    const data = JSON.parse(e.data);
    renderFleetNodes(data.nodes || []);
    renderFleetJobs(data.jobs || []);
  };
  _fleetWs.onclose = () => setTimeout(fleetConnectWs, 3000);
}

function renderFleetNodes(nodes) {
  const strip = document.getElementById("fleet-node-strip");
  if (!strip) return;
  const statusEl = document.getElementById("fleet-discovery-status");
  if (statusEl) {
    statusEl.textContent = `${nodes.length} node${nodes.length !== 1 ? "s" : ""} registered · ${nodes.filter(n => n.reachable).length} online`;
  }
  // Preserve selection
  strip.innerHTML = "";
  if (nodes.length === 0) {
    strip.innerHTML = '<div class="dim" style="font-size:9px;padding:8px">No fleet nodes. Click SCAN to discover or register via API.</div>';
    return;
  }
  nodes.forEach(n => {
    const pill = document.createElement("button");
    pill.className = "mode-btn" + (_fleetSelectedNode === n.id ? " active" : "");
    pill.dataset.nodeId = n.id;
    const dot = n.reachable ? "●" : "○";
    const dotColor = n.reachable ? "var(--green)" : "var(--red)";
    pill.innerHTML = `<span style="color:${dotColor}">${dot}</span> ${_esc(n.name)}`;
    pill.addEventListener("click", () => fleetSelectNode(n.id));
    strip.appendChild(pill);
  });
  // If selected node still exists, refresh its detail
  if (_fleetSelectedNode) {
    const selected = nodes.find(n => n.id === _fleetSelectedNode);
    if (selected) updateFleetRemoteHeader(selected);
  }
}

async function fleetSelectNode(nodeId) {
  _fleetSelectedNode = nodeId;
  // Re-render strip to show active state
  document.querySelectorAll("#fleet-node-strip .mode-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.nodeId === nodeId);
  });
  // Show ping/remove buttons
  document.getElementById("fleet-remote-ping").classList.remove("hidden");
  document.getElementById("fleet-remote-remove").classList.remove("hidden");
  // Fetch full node detail with live status
  try {
    const r = await _fetch(`${OLLMO_API}/extensions/fleet/nodes/${nodeId}`);
    const node = await r.json();
    if (node.error) { showAlert("warning", node.error); return; }
    updateFleetRemoteHeader(node);
    renderFleetRemoteModes(node);
    renderFleetRemoteServices(node);
  } catch (e) {
    showAlert("warning", "Failed to fetch node: " + e.message);
  }
}

function updateFleetRemoteHeader(node) {
  document.getElementById("fleet-remote-name").textContent = node.name || node.id;
  const statusEl = document.getElementById("fleet-remote-status");
  statusEl.textContent = node.reachable ? "ONLINE" : "OFFLINE";
  statusEl.style.color = node.reachable ? "var(--green)" : "var(--red)";
  const vramEl = document.getElementById("fleet-remote-vram");
  const ramEl = document.getElementById("fleet-remote-ram");
  if (node.live?.vram) {
    vramEl.textContent = `${node.live.vram.used_gb}/${node.live.vram.total_gb} GB VRAM`;
  } else if (node.vram_snapshot) {
    vramEl.textContent = `${node.vram_snapshot.used_gb || "?"}/${node.vram_snapshot.total_gb || "?"} GB VRAM`;
  } else {
    vramEl.textContent = "";
  }
  if (node.live?.ram) {
    ramEl.textContent = `${node.live.ram.used_gb}/${node.live.ram.total_gb} GB RAM`;
  } else {
    ramEl.textContent = "";
  }
}

function renderFleetRemoteModes(node) {
  const container = document.getElementById("fleet-remote-modes");
  if (!container) return;
  const modesData = node.live?.modes;
  const activeModes = node.live?.active_modes || [];
  if (!modesData || typeof modesData !== "object") {
    container.innerHTML = '<div class="dim" style="font-size:9px;padding:8px">No mode data available</div>';
    return;
  }
  // modesData could be {modes: {...}} or just {...}
  const modes = modesData.modes || modesData;
  const entries = Object.entries(modes);
  if (entries.length === 0) {
    container.innerHTML = '<div class="dim" style="font-size:9px;padding:8px">No modes configured on this node</div>';
    return;
  }
  container.innerHTML = '<div class="fleet-section-label">MODES</div>';
  const grid = document.createElement("div");
  grid.className = "fleet-mode-grid";
  entries.forEach(([key, mode]) => {
    const isActive = activeModes.includes(key);
    const card = document.createElement("div");
    card.className = "fleet-mode-card" + (isActive ? " active" : "");
    const name = mode.name || key;
    const svcList = (mode.services || []).map(s => _esc(s)).join(", ");
    card.innerHTML =
      `<div class="fleet-mode-header">` +
        `<span class="fleet-mode-name">${_esc(name)}</span>` +
        `<span class="fleet-mode-status" style="color:${isActive ? "var(--green)" : "var(--text-dim)"}">${isActive ? "ACTIVE" : "IDLE"}</span>` +
      `</div>` +
      `<div class="fleet-mode-meta">` +
        `<span class="dim">${_esc(mode.description || "")}</span>` +
      `</div>` +
      `<div class="fleet-mode-meta">` +
        `<span class="dim">${mode.vram_budget_gb ? mode.vram_budget_gb + " GB budget" : ""}</span>` +
        `<span class="dim">${svcList ? " · " + svcList : ""}</span>` +
      `</div>` +
      `<div class="fleet-mode-actions">` +
        `<button class="mode-btn fleet-mode-activate" data-mode="${_esc(key)}"${isActive ? " disabled" : ""}>ACTIVATE</button>` +
        `<button class="mode-btn fleet-mode-deactivate" data-mode="${_esc(key)}"${!isActive ? " disabled" : ""}>DEACTIVATE</button>` +
      `</div>`;
    grid.appendChild(card);
  });
  container.appendChild(grid);
  // Wire buttons
  container.querySelectorAll(".fleet-mode-activate").forEach(btn => {
    btn.addEventListener("click", () => fleetDispatchJob("mode_activate", btn.dataset.mode));
  });
  container.querySelectorAll(".fleet-mode-deactivate").forEach(btn => {
    btn.addEventListener("click", () => fleetDispatchJob("mode_deactivate", btn.dataset.mode));
  });
}

function renderFleetRemoteServices(node) {
  const container = document.getElementById("fleet-remote-services");
  if (!container) return;
  const services = node.live?.services || node.capabilities || {};
  if (!services || Object.keys(services).length === 0) {
    container.innerHTML = '<div class="dim" style="font-size:9px;padding:8px">No service data available. Node may be offline.</div>';
    return;
  }
  container.innerHTML = '<div class="fleet-section-label">SERVICES</div>';
  // If services is from capabilities (config/services response), it has service config
  // If from live.services, it has status info
  const entries = Array.isArray(services) ? services : Object.entries(services);

  const isConfigFormat = !Array.isArray(services) && typeof services === "object";

  if (isConfigFormat) {
    Object.entries(services).forEach(([name, svc]) => {
      const card = document.createElement("div");
      card.className = "svc-card";
      const status = svc.status || "unknown";
      const statusColor = status === "running" ? "var(--green)" : status === "stopped" || status === "exited" ? "var(--red)" : "var(--yellow)";
      card.innerHTML =
        `<div class="svc-card-header">` +
          `<span class="svc-card-name">${_esc(name)}</span>` +
          `<span style="color:${statusColor};font-size:9px">${_esc(status.toUpperCase())}</span>` +
        `</div>` +
        `<div class="svc-card-body">` +
          `<span class="dim" style="font-size:9px">${_esc(svc.description || "")}` +
          `${svc.port ? " · :" + _esc(svc.port) : ""}` +
          `${svc.vram_est_gb ? " · " + _esc(svc.vram_est_gb) + "GB VRAM" : ""}</span>` +
        `</div>` +
        `<div class="svc-card-actions">` +
          `<button class="mode-btn fleet-svc-start" data-svc="${_esc(name)}">START</button>` +
          `<button class="mode-btn fleet-svc-stop" data-svc="${_esc(name)}">STOP</button>` +
        `</div>`;
      container.appendChild(card);
    });
  }

  // Wire up start/stop buttons
  container.querySelectorAll(".fleet-svc-start").forEach(btn => {
    btn.addEventListener("click", () => fleetDispatchJob("service_start", btn.dataset.svc));
  });
  container.querySelectorAll(".fleet-svc-stop").forEach(btn => {
    btn.addEventListener("click", () => fleetDispatchJob("service_stop", btn.dataset.svc));
  });
}

async function fleetDispatchJob(type, target) {
  if (!_fleetSelectedNode) return;
  try {
    const r = await _fetch(`${OLLMO_API}/extensions/fleet/jobs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_id: _fleetSelectedNode, type, target }),
    });
    const job = await r.json();
    if (job.error) { showAlert("warning", job.error); return; }
    showAlert(job.status === "complete" ? "info" : "warning", `${type} ${target}: ${job.status}`);
    // Refresh node view
    fleetSelectNode(_fleetSelectedNode);
  } catch (e) {
    showAlert("warning", "Job dispatch failed: " + e.message);
  }
}

function renderFleetJobs(jobs) {
  const container = document.getElementById("fleet-jobs-list");
  if (!container) return;
  if (!jobs || jobs.length === 0) {
    container.innerHTML = '<div class="dim" style="font-size:9px;padding:8px">No jobs dispatched</div>';
    return;
  }
  container.innerHTML = "";
  jobs.slice(0, 20).forEach(j => {
    const row = document.createElement("div");
    row.className = "fleet-job-row";
    const statusColor = j.status === "complete" ? "var(--green)" : j.status === "failed" ? "var(--red)" : "var(--yellow)";
    const time = j.created_at ? new Date(j.created_at).toLocaleTimeString() : "";
    row.innerHTML =
      `<span style="color:${statusColor};font-size:9px;width:60px;display:inline-block">${_esc(j.status?.toUpperCase())}</span>` +
      `<span style="font-size:9px;width:80px;display:inline-block">${_esc(j.type)}</span>` +
      `<span style="font-size:9px;width:80px;display:inline-block">${_esc(j.target || "—")}</span>` +
      `<span class="dim" style="font-size:9px;width:70px;display:inline-block">${_esc(j.node_name)}</span>` +
      `<span class="dim" style="font-size:9px">${_esc(time)}</span>`;
    container.appendChild(row);
  });
}

async function fleetScan() {
  try {
    const r = await _fetch(`${OLLMO_API}/extensions/fleet/discover`, { method: "POST" });
    const d = await r.json();
    showAlert("info", `Discovery: found ${d.discovered} new node${d.discovered !== 1 ? "s" : ""}`);
  } catch (e) {
    showAlert("warning", "Scan failed: " + e.message);
  }
}

async function fleetPingSelected() {
  if (!_fleetSelectedNode) return;
  try {
    const r = await _fetch(`${OLLMO_API}/extensions/fleet/nodes/${_fleetSelectedNode}/ping`, { method: "POST" });
    const d = await r.json();
    showAlert("info", `Ping: ${d.reachable ? "reachable" : "unreachable"}`);
  } catch (e) {
    showAlert("warning", "Ping failed: " + e.message);
  }
}

async function fleetRemoveSelected() {
  if (!_fleetSelectedNode) return;
  try {
    await _fetch(`${OLLMO_API}/extensions/fleet/nodes/${_fleetSelectedNode}`, { method: "DELETE" });
    _fleetSelectedNode = null;
    document.getElementById("fleet-remote-name").textContent = "Select a node";
    document.getElementById("fleet-remote-status").textContent = "";
    document.getElementById("fleet-remote-vram").textContent = "";
    document.getElementById("fleet-remote-ram").textContent = "";
    document.getElementById("fleet-remote-modes").innerHTML = '<div class="dim" style="font-size:9px;padding:8px">Select a fleet node to view its modes</div>';
    document.getElementById("fleet-remote-services").innerHTML = '<div class="dim" style="font-size:9px;padding:8px">Select a fleet node to view its services</div>';
    document.getElementById("fleet-remote-ping").classList.add("hidden");
    document.getElementById("fleet-remote-remove").classList.add("hidden");
  } catch (e) {
    showAlert("warning", "Remove failed: " + e.message);
  }
}

async function fleetLoadSettings() {
  try {
    const r = await _fetch(`${OLLMO_API}/extensions/fleet/config`);
    const cfg = await r.json();
    document.getElementById("fleet-hb-mode").value = cfg.heartbeat_mode || "both";
    document.getElementById("fleet-hb-interval").value = cfg.heartbeat_interval || 30;
    document.getElementById("fleet-stale-after").value = cfg.stale_after || 90;
    document.getElementById("fleet-discovery-toggle").checked = cfg.discovery_enabled !== false;
    document.getElementById("fleet-disc-interval").value = cfg.discovery_interval || 15;
  } catch {}
}

async function fleetSaveSettings() {
  const body = {
    heartbeat_mode: document.getElementById("fleet-hb-mode").value,
    heartbeat_interval: parseInt(document.getElementById("fleet-hb-interval").value),
    stale_after: parseInt(document.getElementById("fleet-stale-after").value),
    discovery_enabled: document.getElementById("fleet-discovery-toggle").checked,
    discovery_interval: parseInt(document.getElementById("fleet-disc-interval").value),
  };
  try {
    const r = await _fetch(`${OLLMO_API}/extensions/fleet/config`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (d.error) { showAlert("warning", "Save failed: " + JSON.stringify(d.error)); return; }
    showAlert("info", "Fleet settings saved");
  } catch (e) {
    showAlert("warning", "Save failed: " + e.message);
  }
}

// --- Settings ---
const SETTINGS_KEY = "oaio-settings";
const DEFAULT_SETTINGS = {
  configPanels: true,
  gridDensity: "normal",
  fontScale: 100,
  textSize: 13,
  buttonSize: "normal",
  canvasHeight: 500,
  accentColor: "#e10600",
  bgIntensity: 4,
  animations: true,
  bgImage: null,
  bgImageName: null,
  bgOpacity: 15,
  bgBlur: 0,
  bgSize: "cover",
  bgPosition: "center center",
  autoStrips: false,
  pollInterval: 3000,
  tierSata: "#ffa726",
  tierNvme: "#2196f3",
  tierRam: "#00e676",
  tierVram: "#ab47bc",
  grpLlm: "#42a5f5",
  grpAudio: "#ffa726",
  grpRender: "#66bb6a",
  grpControl: "#78909c",
  modeOllmo: "#e879f9",
  modeOaudio: "#22d3ee",
  modeComfyui: "#facc15",
  colorGreen: "#00e676",
  colorYellow: "#ffd740",
  colorRed: "#ff1744",
  colorCyan: "#00d2be",
  colorPurple: "#a855f7",
  colorBorder: "#252525",
  colorText: "#e8e8e8",
  colorTextDim: "#555555",
  tierSataBg: "#2a1e00",
  tierNvmeBg: "#0d1a2f",
  tierRamBg: "#0a2a14",
  tierVramBg: "#1f0d2a",
};

let _settings = { ...DEFAULT_SETTINGS };

function loadSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (raw) Object.assign(_settings, JSON.parse(raw));
  } catch {}
}

function saveSettings() {
  try {
    // Don't persist bgImage in main key if too large — use separate key
    const toSave = { ..._settings };
    if (toSave.bgImage && toSave.bgImage.length > 500000) {
      localStorage.setItem(SETTINGS_KEY + "-bg", toSave.bgImage);
      toSave.bgImage = "__separate__";
    } else {
      localStorage.removeItem(SETTINGS_KEY + "-bg");
    }
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(toSave));
  } catch (e) {
    console.warn("Settings save failed:", e.message);
  }
}

function applySettings() {
  const s = _settings;
  const root = document.documentElement;
  const body = document.body;

  // Accent color
  root.style.setProperty("--accent", s.accentColor);

  // Background intensity (0-20 → lightness 0%-7%)
  const l = s.bgIntensity * 0.35;
  root.style.setProperty("--bg", `hsl(0,0%,${l}%)`);
  root.style.setProperty("--bg2", `hsl(0,0%,${l + 2.5}%)`);
  root.style.setProperty("--bg3", `hsl(0,0%,${l + 5}%)`);

  // Font scale + text size
  const baseSize = s.textSize || 13;
  root.style.fontSize = (s.fontScale / 100) * baseSize + "px";

  // Grid density
  body.classList.remove("density-compact", "density-spacious");
  if (s.gridDensity === "compact") body.classList.add("density-compact");
  if (s.gridDensity === "spacious") body.classList.add("density-spacious");

  // Button size
  body.classList.remove("btn-small", "btn-large");
  if (s.buttonSize === "small") body.classList.add("btn-small");
  if (s.buttonSize === "large") body.classList.add("btn-large");

  // Animations
  body.classList.toggle("no-animations", !s.animations);

  // Canvas height
  const wrap = document.getElementById("canvas-wrap");
  if (wrap) wrap.style.height = s.canvasHeight + "px";

  // Config panels toggle
  const configTab = document.getElementById("tab-config");
  if (configTab) configTab.classList.toggle("config-panels-hidden", !s.configPanels);

  // Background image
  const bgLayer = document.getElementById("bg-image-layer");
  if (bgLayer) {
    if (s.bgImage) {
      bgLayer.style.backgroundImage = `url(${s.bgImage})`;
      bgLayer.style.backgroundSize = s.bgSize;
      bgLayer.style.backgroundPosition = s.bgPosition;
      bgLayer.style.filter = s.bgBlur > 0 ? `blur(${s.bgBlur}px)` : "none";
      bgLayer.style.setProperty("--bg-opacity", s.bgOpacity / 100);
      bgLayer.style.opacity = s.bgOpacity / 100;
      bgLayer.classList.add("active");
    } else {
      bgLayer.style.backgroundImage = "";
      bgLayer.classList.remove("active");
    }
  }

  // Auto-show strips (telemetry + accounting together)
  if (s.autoStrips) {
    document.getElementById("telemetry-strip").style.display = "";
    document.getElementById("acct-strip").style.display = "";
    document.querySelectorAll(".sec-toggle").forEach(btn => btn.classList.add("active"));
  }

  // Infographic colors — storage tiers
  root.style.setProperty("--tier-sata", s.tierSata);
  root.style.setProperty("--tier-nvme", s.tierNvme);
  root.style.setProperty("--tier-ram", s.tierRam);
  root.style.setProperty("--tier-vram", s.tierVram);
  // Infographic colors — service groups
  root.style.setProperty("--grp-llm", s.grpLlm);
  root.style.setProperty("--grp-audio", s.grpAudio);
  root.style.setProperty("--grp-render", s.grpRender);
  root.style.setProperty("--grp-control", s.grpControl);
  // Infographic colors — modes
  root.style.setProperty("--mode-ollmo", s.modeOllmo);
  root.style.setProperty("--mode-oaudio", s.modeOaudio);
  root.style.setProperty("--mode-comfyui", s.modeComfyui);
  // Status + base colors
  root.style.setProperty("--green", s.colorGreen);
  root.style.setProperty("--yellow", s.colorYellow);
  root.style.setProperty("--red", s.colorRed);
  root.style.setProperty("--cyan", s.colorCyan);
  root.style.setProperty("--purple", s.colorPurple);
  root.style.setProperty("--border", s.colorBorder);
  root.style.setProperty("--text", s.colorText);
  root.style.setProperty("--text-dim", s.colorTextDim);
  // Tier tinted backgrounds
  root.style.setProperty("--tier-sata-bg", s.tierSataBg);
  root.style.setProperty("--tier-nvme-bg", s.tierNvmeBg);
  root.style.setProperty("--tier-ram-bg", s.tierRamBg);
  root.style.setProperty("--tier-vram-bg", s.tierVramBg);
}

function syncSettingsUI() {
  const s = _settings;
  const el = (id) => document.getElementById(id);
  el("set-config-panels").checked = s.configPanels;
  el("set-grid-density").value = s.gridDensity;
  el("set-font-scale").value = s.fontScale;
  el("set-font-scale-val").textContent = s.fontScale + "%";
  el("set-text-size").value = s.textSize || 13;
  el("set-text-size-val").textContent = (s.textSize || 13) + "px";
  el("set-button-size").value = s.buttonSize || "normal";
  el("set-canvas-height").value = s.canvasHeight;
  el("set-canvas-height-val").textContent = s.canvasHeight + "px";
  el("set-accent-color").value = s.accentColor;
  el("set-bg-intensity").value = s.bgIntensity;
  el("set-bg-intensity-val").textContent = s.bgIntensity;
  el("set-animations").checked = s.animations;
  el("set-bg-opacity").value = s.bgOpacity;
  el("set-bg-opacity-val").textContent = s.bgOpacity + "%";
  el("set-bg-blur").value = s.bgBlur;
  el("set-bg-blur-val").textContent = s.bgBlur + "px";
  el("set-bg-size").value = s.bgSize;
  el("set-bg-position").value = s.bgPosition;
  el("set-bg-filename").textContent = s.bgImageName || "No image";
  el("set-auto-strips").checked = s.autoStrips;
  el("set-poll-interval").value = s.pollInterval;
  // Infographic colors
  el("set-tier-sata").value = s.tierSata;
  el("set-tier-nvme").value = s.tierNvme;
  el("set-tier-ram").value = s.tierRam;
  el("set-tier-vram").value = s.tierVram;
  el("set-grp-llm").value = s.grpLlm;
  el("set-grp-audio").value = s.grpAudio;
  el("set-grp-render").value = s.grpRender;
  el("set-grp-control").value = s.grpControl;
  el("set-mode-ollmo").value = s.modeOllmo;
  el("set-mode-oaudio").value = s.modeOaudio;
  el("set-mode-comfyui").value = s.modeComfyui;
  el("set-color-green").value = s.colorGreen;
  el("set-color-yellow").value = s.colorYellow;
  el("set-color-red").value = s.colorRed;
  el("set-color-cyan").value = s.colorCyan;
  el("set-color-purple").value = s.colorPurple;
  el("set-color-border").value = s.colorBorder;
  el("set-color-text").value = s.colorText;
  el("set-color-text-dim").value = s.colorTextDim;
  el("set-tier-sata-bg").value = s.tierSataBg;
  el("set-tier-nvme-bg").value = s.tierNvmeBg;
  el("set-tier-ram-bg").value = s.tierRamBg;
  el("set-tier-vram-bg").value = s.tierVramBg;

  // Preview
  const preview = el("set-bg-preview");
  preview.style.backgroundImage = s.bgImage ? `url(${s.bgImage})` : "";

  // Workflow list
  renderWorkflowList();
}

function _settingChanged(key, val) {
  _settings[key] = val;
  saveSettings();
  applySettings();
}

function initSettingsHandlers() {
  const bind = (id, key, transform) => {
    const el = document.getElementById(id);
    if (!el) return;
    const evt = el.type === "checkbox" ? "change" : "input";
    el.addEventListener(evt, () => {
      const val = el.type === "checkbox" ? el.checked :
                  transform ? transform(el.value) : el.value;
      _settingChanged(key, val);
      // Update display labels
      const label = document.getElementById(id + "-val");
      if (label) {
        if (key === "fontScale") label.textContent = val + "%";
        if (key === "textSize") label.textContent = val + "px";
        if (key === "canvasHeight") label.textContent = val + "px";
        if (key === "bgIntensity") label.textContent = val;
        if (key === "bgOpacity") label.textContent = val + "%";
        if (key === "bgBlur") label.textContent = val + "px";
      }
    });
  };

  bind("set-config-panels", "configPanels");
  bind("set-grid-density", "gridDensity");
  bind("set-font-scale", "fontScale", Number);
  bind("set-text-size", "textSize", Number);
  bind("set-button-size", "buttonSize");
  bind("set-canvas-height", "canvasHeight", Number);
  bind("set-accent-color", "accentColor");
  bind("set-bg-intensity", "bgIntensity", Number);
  bind("set-animations", "animations");
  bind("set-bg-opacity", "bgOpacity", Number);
  bind("set-bg-blur", "bgBlur", Number);
  bind("set-bg-size", "bgSize");
  bind("set-bg-position", "bgPosition");
  bind("set-auto-strips", "autoStrips");
  bind("set-poll-interval", "pollInterval", Number);
  // Infographic colors
  bind("set-tier-sata", "tierSata");
  bind("set-tier-nvme", "tierNvme");
  bind("set-tier-ram", "tierRam");
  bind("set-tier-vram", "tierVram");
  bind("set-grp-llm", "grpLlm");
  bind("set-grp-audio", "grpAudio");
  bind("set-grp-render", "grpRender");
  bind("set-grp-control", "grpControl");
  bind("set-mode-ollmo", "modeOllmo");
  bind("set-mode-oaudio", "modeOaudio");
  bind("set-mode-comfyui", "modeComfyui");
  bind("set-color-green", "colorGreen");
  bind("set-color-yellow", "colorYellow");
  bind("set-color-red", "colorRed");
  bind("set-color-cyan", "colorCyan");
  bind("set-color-purple", "colorPurple");
  bind("set-color-border", "colorBorder");
  bind("set-color-text", "colorText");
  bind("set-color-text-dim", "colorTextDim");
  bind("set-tier-sata-bg", "tierSataBg");
  bind("set-tier-nvme-bg", "tierNvmeBg");
  bind("set-tier-ram-bg", "tierRamBg");
  bind("set-tier-vram-bg", "tierVramBg");

  // Boot with system — API-backed, not localStorage
  const bootEl = document.getElementById("set-boot-with-system");
  if (bootEl) {
    _fetch(`${OLLMO_API}/config/boot`).then(r => r.json()).then(d => {
      bootEl.checked = d.enabled !== false;
    }).catch(() => {});
    bootEl.addEventListener("change", () => {
      _fetch(`${OLLMO_API}/config/boot`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: bootEl.checked }),
      }).then(r => r.json()).then(d => {
        if (d.error) { showAlert("warning", d.error); bootEl.checked = !bootEl.checked; return; }
        showAlert("info", `Boot with system ${d.enabled ? "enabled" : "disabled"}`);
      }).catch(e => {
        showAlert("warning", "Failed to update boot setting: " + e.message);
        bootEl.checked = !bootEl.checked;
      });
    });
  }

  // Background image upload
  document.getElementById("set-bg-upload-btn").addEventListener("click", () => {
    document.getElementById("set-bg-file").click();
  });

  document.getElementById("set-bg-file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      _settings.bgImage = ev.target.result;
      _settings.bgImageName = file.name;
      saveSettings();
      applySettings();
      syncSettingsUI();
    };
    reader.readAsDataURL(file);
  });

  document.getElementById("set-bg-clear-btn").addEventListener("click", () => {
    _settings.bgImage = null;
    _settings.bgImageName = null;
    localStorage.removeItem(SETTINGS_KEY + "-bg");
    saveSettings();
    applySettings();
    syncSettingsUI();
  });

  // Export
  document.getElementById("set-export-btn").addEventListener("click", () => {
    const data = { ..._settings };
    delete data.bgImage; // Don't export large image data
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "oaio-settings.json";
    a.click();
    URL.revokeObjectURL(url);
  });

  // Import
  document.getElementById("set-import-btn").addEventListener("click", () => {
    document.getElementById("set-import-file").click();
  });

  document.getElementById("set-import-file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      try {
        const imported = JSON.parse(ev.target.result);
        // Whitelist: only allow known setting keys to prevent prototype pollution / injection
        const allowedKeys = new Set(Object.keys(DEFAULT_SETTINGS));
        const sanitized = {};
        for (const key of Object.keys(imported)) {
          if (allowedKeys.has(key)) sanitized[key] = imported[key];
        }
        Object.assign(_settings, sanitized);
        saveSettings();
        applySettings();
        syncSettingsUI();
        showAlert("warning", "Settings imported successfully");
      } catch {
        showAlert("critical", "Import failed — invalid settings file");
      }
    };
    reader.readAsText(file);
  });

  // Reset
  document.getElementById("set-reset-btn").addEventListener("click", () => {
    if (!confirm("Reset all settings to defaults?")) return;
    Object.assign(_settings, DEFAULT_SETTINGS);
    _settings.bgImage = null;
    _settings.bgImageName = null;
    localStorage.removeItem(SETTINGS_KEY);
    localStorage.removeItem(SETTINGS_KEY + "-bg");
    saveSettings();
    applySettings();
    syncSettingsUI();
  });

  // Poll interval change — restart timers
  document.getElementById("set-poll-interval").addEventListener("change", () => {
    if (_liveMonitorPollTimer) clearInterval(_liveMonitorPollTimer);
    _liveMonitorPollTimer = setInterval(fetchLiveMonitorStats, _settings.pollInterval);
    if (_monitorPollTimer) clearInterval(_monitorPollTimer);
    _monitorPollTimer = setInterval(fetchMonitorStats, _settings.pollInterval);
  });

  // Workflow save
  document.getElementById("set-wf-save-btn").addEventListener("click", () => {
    const nameEl = document.getElementById("set-wf-name");
    const name = nameEl.value.trim();
    if (!name) return;
    saveWorkflow(name);
    nameEl.value = "";
    renderWorkflowList();
  });

  // API Token management
  const tokenInput = document.getElementById('set-api-token');
  if (tokenInput) {
    tokenInput.value = _apiToken ? '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022' : '';
    document.getElementById('set-api-token-save').addEventListener('click', () => {
      const val = tokenInput.value.trim();
      if (val && val !== '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022') {
        _apiToken = val;
        localStorage.setItem('oaio-api-token', _apiToken);
        tokenInput.value = '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022';
        showAlert('info', 'API token saved');
      }
    });
    document.getElementById('set-api-token-clear').addEventListener('click', () => {
      _apiToken = '';
      localStorage.removeItem('oaio-api-token');
      tokenInput.value = '';
      showAlert('info', 'API token cleared');
    });
  }
}

// --- Workflow Saves ---
const WORKFLOWS_KEY = "oaio-workflows";

function getWorkflows() {
  try {
    return JSON.parse(localStorage.getItem(WORKFLOWS_KEY) || "{}");
  } catch { return {}; }
}

async function saveWorkflow(name) {
  const workflows = getWorkflows();

  // Capture current state
  const state = {
    settings: { ..._settings },
    timestamp: Date.now(),
  };
  delete state.settings.bgImage; // too large

  // Capture graph layout if available
  if (graph) {
    state.graphData = graph.serialize();
  }

  // Capture active modes
  try {
    const r = await _fetch(`${OLLMO_API}/system/status`);
    const d = await r.json();
    state.activeModes = d.active_modes || [];
  } catch {}

  // Capture routing
  try {
    const r = await _fetch(`${OLLMO_API}/config/routing`);
    state.routing = await r.json();
  } catch {}

  workflows[name] = state;
  localStorage.setItem(WORKFLOWS_KEY, JSON.stringify(workflows));
}

async function loadWorkflow(name) {
  const workflows = getWorkflows();
  const state = workflows[name];
  if (!state) return;

  // Restore settings (preserve bgImage from current)
  const currentBg = _settings.bgImage;
  const currentBgName = _settings.bgImageName;
  Object.assign(_settings, state.settings);
  _settings.bgImage = currentBg;
  _settings.bgImageName = currentBgName;
  saveSettings();
  applySettings();
  syncSettingsUI();

  // Restore routing
  if (state.routing) {
    try {
      await _fetch(`${OLLMO_API}/config/routing`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state.routing),
      });
    } catch {}
  }

  // Restore graph layout
  if (state.graphData && graph) {
    graph.configure(state.graphData);
    canvas?.setDirty(true, true);
    scheduleGraphSave();
  }

  // Restore active modes
  if (state.activeModes && state.activeModes.length > 0) {
    for (const mode of state.activeModes) {
      try {
        await _fetch(`${OLLMO_API}/modes/${encodeURIComponent(mode)}/activate`, { method: "POST" });
      } catch {}
    }
  }
}

function deleteWorkflow(name) {
  const workflows = getWorkflows();
  delete workflows[name];
  localStorage.setItem(WORKFLOWS_KEY, JSON.stringify(workflows));
  renderWorkflowList();
}

function renderWorkflowList() {
  const container = document.getElementById("set-wf-list");
  if (!container) return;
  const workflows = getWorkflows();
  const names = Object.keys(workflows);

  if (names.length === 0) {
    container.innerHTML = '<span class="dim">No saved workflows</span>';
    return;
  }

  container.innerHTML = names.map(name => {
    const wf = workflows[name];
    const date = new Date(wf.timestamp).toLocaleDateString("en-US", {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
    });
    return `<div class="wf-item">
      <span class="wf-item-name">${_esc(name)}</span>
      <span class="wf-item-date">${date}</span>
      <button class="mode-btn wf-load-btn" data-wf="${_esc(name)}">LOAD</button>
      <button class="mode-btn wf-del-btn" data-wf="${_esc(name)}">DEL</button>
    </div>`;
  }).join("");

  // Wire up workflow button listeners (avoid inline onclick with user data)
  container.querySelectorAll('.wf-load-btn').forEach(btn => btn.addEventListener('click', () => loadWorkflow(btn.dataset.wf)));
  container.querySelectorAll('.wf-del-btn').forEach(btn => btn.addEventListener('click', () => deleteWorkflow(btn.dataset.wf)));
}

// --- Init ---
loadSettings();
// Migration: reset configPanels to default
if (!localStorage.getItem(SETTINGS_KEY + "-v3")) {
  _settings.configPanels = true;
  localStorage.setItem(SETTINGS_KEY + "-v3", "1");
  saveSettings();
}
// Restore separate bg image if needed
try {
  if (_settings.bgImage === "__separate__") {
    _settings.bgImage = localStorage.getItem(SETTINGS_KEY + "-bg");
  }
} catch {}
applySettings();
initSettingsHandlers();
syncSettingsUI();

fetchModesData().then(() => { renderModeGrid(); renderModePills(); renderModeStrip(); });
fetchServicesCfg();
fetchPathsForRamTier();
loadTemplates();
connectStatusWS();
startLiveMonitor();
initDebuggerCard();
setInterval(pollStorage, 30000);
pollStorage();

// Immediate HTTP poll so gauges show values before first WS tick
poll();

// Redraw benchmark canvas on resize (fixes stretch)
const _benchEl = document.getElementById("bench-canvas");
if (_benchEl) {
  new ResizeObserver(() => _redrawBench()).observe(_benchEl);
}
// Also redraw on window resize
window.addEventListener("resize", _redrawBench);

// ── Canvas hamburger menu (collapse on narrow canvas) ──
(function initCanvasHamburger() {
  const hamBtn = document.getElementById("canvas-hamburger");
  const hamMenu = document.getElementById("canvas-ham-menu");
  if (!hamBtn || !hamMenu) return;

  // Populate hamburger menu sections by cloning originals
  function syncHamMenu() {
    const fSec = document.getElementById("ham-filters");
    const cSec = document.getElementById("ham-actions-center");
    const rSec = document.getElementById("ham-actions-right");
    if (!fSec || !cSec || !rSec) return;

    // Filters: clone mode-filter pills
    const mf = document.getElementById("mode-filter");
    if (mf) {
      fSec.innerHTML = "";
      mf.querySelectorAll(".grp-pill").forEach(btn => {
        const cl = btn.cloneNode(true);
        cl.addEventListener("click", () => { btn.click(); syncHamMenu(); });
        fSec.appendChild(cl);
      });
    }

    // Center: proxy SAVE MODE / SAVE TEMPLATE
    cSec.innerHTML = "";
    ["mode-strip-add", "mode-strip-save-tpl"].forEach(id => {
      const orig = document.getElementById(id);
      if (!orig) return;
      const cl = orig.cloneNode(true);
      cl.removeAttribute("id");
      cl.addEventListener("click", () => { orig.click(); hamBtn.click(); });
      cSec.appendChild(cl);
    });

    // Right: proxy + NODE, + MODE, SCAN
    rSec.innerHTML = "";
    ["add-svc-btn", "add-mode-btn", "scan-docker-btn"].forEach(id => {
      const orig = document.getElementById(id);
      if (!orig) return;
      const cl = orig.cloneNode(true);
      cl.removeAttribute("id");
      cl.addEventListener("click", () => { orig.click(); hamBtn.click(); });
      rSec.appendChild(cl);
    });
  }

  hamBtn.addEventListener("click", () => {
    const open = hamMenu.classList.toggle("open");
    hamBtn.classList.toggle("open", open);
    if (open) syncHamMenu();
  });

  // Close menu when clicking outside
  document.addEventListener("click", (e) => {
    if (!hamMenu.contains(e.target) && e.target !== hamBtn) {
      hamMenu.classList.remove("open");
      hamBtn.classList.remove("open");
    }
  });
})();

