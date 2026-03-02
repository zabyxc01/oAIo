/**
 * oAIo — main app
 */

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

function _loadScripts(urls, cb) {
  if (!urls.length) { cb(); return; }
  const s = document.createElement("script");
  s.src    = urls[0];
  s.onload  = () => _loadScripts(urls.slice(1), cb);
  s.onerror = () => { console.warn("Failed to load", urls[0]); _loadScripts(urls.slice(1), cb); };
  document.head.appendChild(s);
}

function initLiteGraph() {
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
    canvas = new LGraphCanvas("#node-canvas", graph);
    canvas.background_image = null;
    canvas.render_shadows   = false;
    canvas.show_info        = false;

    [
      ["oAIo/ollama",     [60,  60]],
      ["oAIo/kokoro-tts", [60,  200]],
      ["oAIo/rvc",        [300, 200]],
      ["oAIo/open-webui", [540, 60]],
      ["oAIo/f5-tts",     [60,  340]],
      ["oAIo/comfyui",    [540, 200]],
    ].forEach(([type, pos]) => {
      const node = LiteGraph.createNode(type);
      if (node) { node.pos = pos; graph.add(node); }
    });

    graph.start();

    // Wire canvas callbacks (must be inside initLiteGraph so canvas is non-null)
    canvas.onNodeDblClick = node => {
      if (node && node._svc) enterContainer(node);
    };

    canvas.onNodeSelected = node => {
      if (!node) return;
      selectedNode = node;
      showConfigView("node");
      document.getElementById("config-node-name").textContent  = node.title || node.type;
      document.getElementById("config-node-group").textContent = node._svc?.group || "";
      document.getElementById("config-vram-est").textContent   =
        node._svc?.vramEst ? `~${node._svc.vramEst}GB VRAM est` : "";
      loadNodeConfig(node);
    };
  } catch (e) {
    console.error("LiteGraph init failed:", e);
  }
}

// --- Timeline canvas ---
const timelineCanvas = document.getElementById("timeline-canvas");

// --- Tab switching ---
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach(t => t.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "advanced") loadAdvancedTab();
    if (btn.dataset.tab === "config")   initLiteGraph();
  });
});

// --- Section toggles (TELEMETRY / ACCOUNTING / TIMELINE) ---
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
    if (sec === "timeline") {
      const card = document.getElementById("card-timeline");
      card.style.display = on ? "" : "none";
      if (on) {
        // Layout must settle before canvas has real dimensions
        requestAnimationFrame(() => {
          if (timelineCanvas.offsetWidth > 0) {
            Timeline.drawTimeline(timelineCanvas, null, activeViews, null);
          }
        });
      }
    }
  });
});

// --- Gauges ---
function setGauge(barId, labelId, used, total) {
  const pct = Math.min(used / total, 1);
  const bar = document.getElementById(barId);
  bar.style.width = (pct * 100) + "%";
  bar.className = "fill" + (pct > 0.85 ? " hot" : pct > 0.6 ? " warn" : "");
  document.getElementById(labelId).textContent = `${used.toFixed(1)} / ${total} GB`;
}

// --- Timeline view toggles ---
const activeViews = new Set();

const PILL_ROWS = {
  nvme: ["nvme_r", "nvme_w"],
  sata: ["sata_r", "sata_w"],
};

document.querySelectorAll(".view-pill").forEach(pill => {
  pill.addEventListener("click", () => {
    const view = pill.dataset.view;
    const rows = PILL_ROWS[view] || [view];
    const allActive = rows.every(r => activeViews.has(r));

    if (allActive) {
      if (activeViews.size > rows.length) {
        rows.forEach(r => activeViews.delete(r));
        pill.classList.remove("active");
      }
    } else {
      rows.forEach(r => activeViews.add(r));
      pill.classList.add("active");
    }
    if (timelineCanvas.offsetWidth > 0) {
      Timeline.drawTimeline(timelineCanvas, null, activeViews, null);
    }
  });
});

// --- Group filter ---
let activeGroup = "all";

document.querySelectorAll(".grp-pill").forEach(pill => {
  pill.addEventListener("click", () => {
    activeGroup = pill.dataset.group;
    document.querySelectorAll(".grp-pill").forEach(p =>
      p.classList.toggle("active", p.dataset.group === activeGroup)
    );
    applyGroupFilter();
  });
});

function applyGroupFilter() {
  if (!graph) return;
  graph.getNodes().forEach(node => {
    const g   = node._svc?.group;
    const dim = activeGroup !== "all" && g !== activeGroup;
    node.color   = dim ? "#222"    : "#1e1e1e";
    node.bgcolor = dim ? "#1a1a1a" : "#141414";
  });
  graph.setDirtyCanvas(true, true);
}

// --- Mode system ---
let activeModes = [];
let modesData   = {};

async function fetchModesData() {
  try {
    const r = await fetch(`${OLLMO_API}/modes`);
    modesData = await r.json();
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
    tab.className = "mode-tab " + (i === 0 ? "primary" : "secondary");
    tab.innerHTML =
      `<span>${label}</span>` +
      `<span class="tab-vram">${vram}GB</span>` +
      `<span class="tab-close" data-id="${modeId}">×</span>`;
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
  const modeOrder = ["converse", "create", "forge", "render_image", "render_video", "render_3d", "render_full"];
  const keys = modeOrder.filter(k => modesData[k]).concat(
    Object.keys(modesData).filter(k => !modeOrder.includes(k))
  );
  keys.forEach(id => {
    const info = modesData[id];
    const idx  = activeModes.indexOf(id);
    const btn  = document.createElement("button");
    btn.className = "mode-card-btn" +
      (idx === 0 ? " primary" : idx === 1 ? " secondary" : "");
    btn.dataset.mode = id;
    btn.innerHTML =
      `<span class="mcb-name">${info?.name || id.toUpperCase()}</span>` +
      `<span class="mcb-vram">${info?.vram_budget_gb ?? info?.vram_est_gb ?? "?"}GB</span>`;
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
    const r = await fetch(`${OLLMO_API}/modes/${modeId}/check`);
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
    await fetch(`${OLLMO_API}/emergency/kill`, { method: "POST" });
    activeModes = [];
    renderModeTabs();
    renderModeGrid();
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
    const r    = await fetch(`${OLLMO_API}/modes/${modeId}/activate`, { method: "POST" });
    const data = await r.json();
    if (data.warning) {
      showAlert("warning",
        `${modeId.toUpperCase()} active — ~${data.projection?.projected_gb}GB projected.`
      );
    }
  } catch {}

  await fetchModesData();
  renderModeGrid();
  loadModeConfig(modeId);
  poll();
});

function deactivateMode(modeId) {
  activeModes = activeModes.filter(m => m !== modeId);
  renderModeTabs();
  renderModeGrid();
  if (selectedNode) loadModeAllocations(selectedNode);
  fetch(`${OLLMO_API}/modes/${modeId}/deactivate`, { method: "POST" }).catch(() => {});
}

// --- Sub-graph navigation ---
const subGraphCache = {};
const navStack      = [];

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
  document.getElementById("group-filter").classList.add("hidden");
  document.getElementById("breadcrumb").classList.remove("hidden");
  document.getElementById("crumb-path").textContent = node.title || svcName.toUpperCase();
}

function exitContainer() {
  if (!canvas || navStack.length === 0) return;
  const prev = navStack.pop();
  canvas.graph.stop();
  canvas.graph = prev.graph;
  canvas.graph.start();
  canvas.draw(true, true);
  if (navStack.length === 0) {
    document.getElementById("group-filter").classList.remove("hidden");
    document.getElementById("breadcrumb").classList.add("hidden");
  } else {
    document.getElementById("crumb-path").textContent = navStack[navStack.length - 1].title;
  }
}

document.getElementById("crumb-back").addEventListener("click", exitContainer);

// --- Config panel scope switching ---
function showConfigView(view) {
  document.getElementById("config-node").classList.toggle("hidden",  view !== "node");
  document.getElementById("config-mode").classList.toggle("hidden",  view !== "mode");
  document.getElementById("config-paths").classList.toggle("hidden", view !== "paths");
}

// --- Load persisted node configs ---
async function initConfigs() {
  try {
    const r = await fetch(`${OLLMO_API}/config/nodes`);
    const data = await r.json();
    if (data.nodeConfigs) Object.assign(nodeConfigs, data.nodeConfigs);
    if (data.modeConfigs) Object.assign(modeConfigs, data.modeConfigs);
  } catch {}
}
initConfigs();

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
  nodeConfigs[key] = {
    memory:   document.getElementById("cfg-memory").value,
    priority: document.getElementById("cfg-priority").value,
    bus:      document.getElementById("cfg-bus").value,
    limit:    document.getElementById("cfg-limit").value,
    boot:     document.getElementById("cfg-boot").checked,
    sub,
  };
  fetch(`${OLLMO_API}/config/nodes`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nodeConfigs }),
  }).catch(() => {});
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

function loadNodeConfig(node) {
  const key = node.title || node.type;
  const cfg = nodeConfigs[key] || {};
  document.getElementById("cfg-memory").value   = cfg.memory   || "vram";
  document.getElementById("cfg-priority").value = cfg.priority || 1;
  document.getElementById("cfg-bus").value      = cfg.bus      || "nvme";
  document.getElementById("cfg-limit").value    = cfg.limit    || "soft";
  document.getElementById("cfg-boot").checked   = cfg.boot     || false;
  updateNodeSubs(cfg.sub);
  loadModeAllocations(node);
}

async function loadModeAllocations(node) {
  const svcName = node._svc?.name;
  const wrap = document.getElementById("mode-allocs");
  const body = document.getElementById("mode-allocs-body");
  if (!svcName || activeModes.length === 0) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
  body.innerHTML = "";
  for (const modeId of activeModes) {
    const modeInfo = modesData[modeId] || {};
    let allocs = {}, budget = modeInfo.vram_est_gb || 20;
    try {
      const r = await fetch(`${OLLMO_API}/modes/${modeId}/allocations`);
      const d = await r.json();
      allocs = d.allocations || {};
      budget = d.vram_budget_gb || budget;
    } catch {}
    const current = allocs[svcName] ?? 0;
    const row = document.createElement("div");
    row.className = "config-field";
    row.innerHTML =
      `<div class="config-row">
         <label>${(modeInfo.name || modeId).toUpperCase()}</label>
         <input type="range" class="alloc-slider"
           data-mode="${modeId}" data-svc="${svcName}"
           min="0" max="${budget}" step="0.5" value="${current}" />
         <span class="slider-val alloc-val">${current} GB</span>
       </div>
       <div class="config-sub alloc-projection" id="alloc-proj-${modeId}"></div>`;
    body.appendChild(row);
    const slider = row.querySelector(".alloc-slider");
    const label  = row.querySelector(".alloc-val");
    slider.addEventListener("input", () => { label.textContent = slider.value + " GB"; });
    slider.addEventListener("change", async () => {
      const r = await fetch(
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
  setSubHtml("sub-priority",
    `<span class="sub-desc">${val} — ${PRIORITY_DESC[val] || ""}</span>`
  );
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
let selectedModeId = null;

function loadModeConfig(modeId) {
  selectedModeId = modeId;
  const cfg  = modeConfigs[modeId] || {};
  const info = modesData[modeId]   || {};
  document.getElementById("config-node-name").textContent  = (info.name || modeId).toUpperCase() + " MODE";
  document.getElementById("config-node-group").textContent = "system allocation";
  document.getElementById("config-vram-est").textContent   =
    info.vram_est_gb ? `~${info.vram_est_gb}GB est` : "";
  const vramVal = cfg.vramBudget ?? info.vram_est_gb ?? 10;
  const ramVal  = cfg.ramBudget  ?? 32;
  document.getElementById("mode-vram").value     = vramVal;
  document.getElementById("mode-vram-val").textContent = vramVal + " GB";
  document.getElementById("mode-ram").value      = ramVal;
  document.getElementById("mode-ram-val").textContent  = ramVal + " GB";
  document.getElementById("mode-priority").value = cfg.priority || "primary";
  document.getElementById("mode-limit").value    = cfg.limit    || "soft";
  document.getElementById("mode-services").textContent = (info.services || []).join(", ") || "—";
  updateModeLimitSub(cfg);
  showConfigView("mode");
}

function saveModeConfig() {
  if (!selectedModeId) return;
  modeConfigs[selectedModeId] = {
    vramBudget: parseFloat(document.getElementById("mode-vram").value),
    ramBudget:  parseFloat(document.getElementById("mode-ram").value),
    priority:   document.getElementById("mode-priority").value,
    limit:      document.getElementById("mode-limit").value,
    limitAct:   document.getElementById("sub-mode-limit-action")?.value,
  };
  fetch(`${OLLMO_API}/config/nodes`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ modeConfigs }),
  }).catch(() => {});
}

function updateModeLimitSub(saved) {
  const val = document.getElementById("mode-limit").value;
  if (val === "soft") {
    setSubHtml("sub-mode-limit", `<span class="sub-desc">Action: warn only</span>`);
  } else {
    const act = saved?.limitAct || "swap";
    setSubHtml("sub-mode-limit",
      `<label>On exceed</label>
       <select id="sub-mode-limit-action"
         style="background:var(--bg3);border:1px solid var(--border);color:var(--text);
                padding:3px 6px;border-radius:3px;font-family:inherit;font-size:10px">
         <option value="kill"  ${act==="kill"  ? "selected":""}>Kill lowest priority</option>
         <option value="swap"  ${act==="swap"  ? "selected":""}>Swap services to RAM</option>
         <option value="pause" ${act==="pause" ? "selected":""}>Pause secondary mode</option>
       </select>`
    );
    document.getElementById("sub-mode-limit-action").addEventListener("change", saveModeConfig);
  }
}

document.getElementById("mode-vram").addEventListener("input", e => {
  document.getElementById("mode-vram-val").textContent = e.target.value + " GB";
  saveModeConfig();
});
document.getElementById("mode-vram").addEventListener("change", async e => {
  if (!selectedModeId) return;
  await fetch(`${OLLMO_API}/modes/${selectedModeId}/budget`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ gb: parseFloat(e.target.value) }),
  });
});
document.getElementById("mode-ram").addEventListener("input", e => {
  document.getElementById("mode-ram-val").textContent = e.target.value + " GB";
  saveModeConfig();
});
document.getElementById("mode-priority").addEventListener("change", saveModeConfig);
document.getElementById("mode-limit").addEventListener("change", () => {
  saveModeConfig();
  updateModeLimitSub(modeConfigs[selectedModeId]);
});

// --- Paths & Routing panel (config-panel, hidden in Tab 2) ---
async function loadPathsPanel() {
  showConfigView("paths");
  document.getElementById("config-node-name").textContent  = "Storage Paths";
  document.getElementById("config-node-group").textContent = "symlinks + routing";
  document.getElementById("config-vram-est").textContent   = "";
  try {
    const [pathsR, routingR] = await Promise.all([
      fetch(`${OLLMO_API}/config/paths`),
      fetch(`${OLLMO_API}/config/routing`),
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
    const tierBadge  = `<span class="tier-badge ${p.tier}">${p.tier}</span>`;
    const ctrs       = (p.containers || []).map(c => `<span class="ctr-badge">${c}</span>`).join("");
    const targetText = p.target || '—';
    entry.innerHTML =
      `<div class="path-row-main">` +
        statusDot +
        `<span class="path-entry-label">${p.label}</span>` +
        tierBadge +
        `<span class="path-entry-target" title="${p.target || ''}">${targetText}</span>` +
        `<button class="path-edit-btn" data-name="${p.name}">EDIT</button>` +
        `<button class="path-del-btn" data-name="${p.name}" title="Remove">✕</button>` +
      `</div>` +
      (ctrs ? `<div class="path-ctrs">${ctrs}</div>` : "");
    entry.querySelector(".path-edit-btn").addEventListener("click", () =>
      startPathEdit(entry, p.name, p.target || p.default_target, onRefresh)
    );
    entry.querySelector(".path-del-btn").addEventListener("click", async () => {
      if (!confirm(`Remove path "${p.label}"?`)) return;
      await fetch(`${OLLMO_API}/config/paths/${encodeURIComponent(p.name)}`, { method: "DELETE" });
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
    await fetch(`${OLLMO_API}/config/paths`, {
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
      await fetch(`${OLLMO_API}/config/paths/${encodeURIComponent(name)}`, {
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
    await fetch(`${OLLMO_API}/config/routing`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    btn.textContent = "SAVED";
  } catch (e) {
    console.warn("save routing error:", e);
    btn.textContent = "ERROR";
  }
  setTimeout(() => { btn.textContent = "APPLY ROUTING"; }, 2000);
});

// --- Live tab card renderers ---

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

// Services card
let _svcsCfg = {};

async function fetchServicesCfg() {
  try {
    const r = await fetch(`${OLLMO_API}/config/services`);
    _svcsCfg = await r.json();
  } catch {}
}

function renderServices(services) {
  const el = document.getElementById("services-list");
  if (!el) return;
  if (!services || services.length === 0) {
    el.innerHTML = '<span class="dim">No services</span>';
    return;
  }
  el.innerHTML = services.map(svc => {
    const cfg     = _svcsCfg[svc.name] || {};
    const vram    = cfg.vram_est_gb || 0;
    const st      = svc.status || "unknown";
    const dot     = st === "running" ? "ok" : (st === "exited" || st === "not_found") ? "err" : "warn";
    const running = st === "running";
    const restore = cfg.auto_restore !== false;
    return `<div class="svc-row">
      <span class="svc-dot ${dot}"></span>
      <span class="svc-name">${svc.name}</span>
      <span class="svc-status">${st}</span>
      <span class="svc-vram">${vram > 0 ? vram + 'GB' : '—'}</span>
      <button class="svc-btn" title="Start" ${running ? 'disabled' : ''}
        onclick="svcAction('${svc.name}','start')">▶</button>
      <button class="svc-btn" title="Stop" ${!running ? 'disabled' : ''}
        onclick="svcAction('${svc.name}','stop')">■</button>
      <label class="svc-restore-toggle" title="${restore ? 'Auto-restore on' : 'Auto-restore off'}">
        <input type="checkbox" ${restore ? 'checked' : ''}
          onchange="svcToggleRestore('${svc.name}', this.checked)">
        <span class="svc-restore-track"></span>
      </label>
    </div>`;
  }).join("");
}

async function svcAction(name, action) {
  try {
    await fetch(`${OLLMO_API}/services/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    setTimeout(poll, 1500);
  } catch {}
}

async function svcToggleRestore(name, enabled) {
  try {
    await fetch(`${OLLMO_API}/config/services/${encodeURIComponent(name)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_restore: enabled }),
    });
    if (_svcsCfg[name]) _svcsCfg[name].auto_restore = enabled;
  } catch {}
}

// RAM tier card
let _pathsCache = [];
let _lastRamTier = null;

async function fetchPathsForRamTier() {
  try {
    const r = await fetch(`${OLLMO_API}/config/paths`);
    _pathsCache = await r.json();
    renderRamTier(_lastRamTier);
  } catch {}
}

function renderRamTier(ramTier) {
  _lastRamTier = ramTier;
  const el = document.getElementById("ramtier-content");
  if (!el) return;

  let headerHTML;
  if (ramTier && Object.keys(ramTier).length > 0 && ramTier.pools) {
    const poolCount = Object.keys(ramTier.pools).length;
    headerHTML = `<div class="acct-stats" style="margin-bottom:8px">
      <span>${poolCount} pool${poolCount !== 1 ? 's' : ''} active</span>
      <span>${ramTier.used_gb?.toFixed(1) ?? 0} / ${ramTier.ceiling_gb ?? '?'} GB</span>
    </div>`;
  } else {
    headerHTML = `<div class="dim" style="margin-bottom:8px; font-size:10px">0 pools active</div>`;
  }

  const pathRows = _pathsCache.map(p => {
    const isRam = p.tier === "ram";
    const tierLabel = isRam ? "RAM → DEF" : (p.tier?.toUpperCase() || "DEF") + " → RAM";
    return `<div class="rt-row">
      <span class="rt-name">${p.label || p.name}</span>
      <button class="rt-btn${isRam ? ' active' : ''}"
        onclick="toggleRamTier('${p.name}',${isRam})">${tierLabel}</button>
    </div>`;
  }).join("");

  el.innerHTML = headerHTML + pathRows;
}

async function toggleRamTier(name, isRam) {
  try {
    await fetch(`${OLLMO_API}/config/paths/${encodeURIComponent(name)}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: isRam ? "default" : "ram" }),
    });
    fetchPathsForRamTier();
  } catch {}
}

// --- Advanced tab ---
async function loadAdvancedTab() {
  try {
    const [pathsR, routingR] = await Promise.all([
      fetch(`${OLLMO_API}/config/paths`),
      fetch(`${OLLMO_API}/config/routing`),
    ]);
    const paths   = await pathsR.json();
    const routing = await routingR.json();
    renderAdvPaths(paths);
    document.getElementById("adv-route-tts-url").value    = routing.tts_url       || "";
    document.getElementById("adv-route-imggen-url").value = routing.image_gen_url || "";
    document.getElementById("adv-route-ollama-url").value = routing.ollama_url    || "";
    document.getElementById("adv-route-stt-url").value    = routing.stt_url       || "";
  } catch (e) {
    console.warn("loadAdvancedTab error:", e);
  }
}

async function reloadAdvPaths() {
  try {
    const r = await fetch(`${OLLMO_API}/config/paths`);
    renderAdvPaths(await r.json());
  } catch {}
}

function renderAdvPaths(paths) {
  const list = document.getElementById("adv-paths-list");
  if (!list) return;
  renderPathsInto(paths, list, reloadAdvPaths);
}

document.getElementById("adv-save-routing-btn").addEventListener("click", async () => {
  const btn  = document.getElementById("adv-save-routing-btn");
  const body = {
    tts_url:       document.getElementById("adv-route-tts-url").value.trim(),
    image_gen_url: document.getElementById("adv-route-imggen-url").value.trim(),
    ollama_url:    document.getElementById("adv-route-ollama-url").value.trim(),
    stt_url:       document.getElementById("adv-route-stt-url").value.trim(),
  };
  try {
    await fetch(`${OLLMO_API}/config/routing`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    btn.textContent = "SAVED";
  } catch {
    btn.textContent = "ERROR";
  }
  setTimeout(() => { btn.textContent = "APPLY ROUTING"; }, 2000);
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
    // Only draw if timeline canvas has been laid out (offsetWidth > 0)
    if (timelineCanvas && timelineCanvas.offsetWidth > 0 && timelineCanvas.offsetHeight > 0) {
      Timeline.drawTimeline(timelineCanvas, d, activeViews);
    }

    // Live tab cards
    if (d.accounting)             renderAccounting(d.accounting);
    if (d.kill_log !== undefined) renderKillLog(d.kill_log);
    if (d.services)               renderServices(d.services);
    if (d.ram_tier !== undefined) renderRamTier(d.ram_tier);

    // Sync active modes from WS
    if (d.active_modes) {
      activeModes = d.active_modes;
      renderModeTabs();
      renderModeGrid();
    }
  } catch (e) {
    console.error("applyStatusUpdate error:", e);
  }
}

// --- WebSocket ---
function connectStatusWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen  = () => {};
  ws.onmessage = event => {
    try {
      const d = JSON.parse(event.data);
      applyStatusUpdate(d);
    } catch(e) { /* ignore parse errors */ }
  };
  ws.onclose = () => {
    setTimeout(connectStatusWS, 3000);
  };
  ws.onerror = () => { ws.close(); };
}

async function pollStorage() {
  try {
    const r = await fetch(`${OLLMO_API}/config/storage/stats`);
    if (r.ok) { window._lastStorageStats = await r.json(); }
  } catch {}
}

async function poll() {
  try {
    const r = await fetch(`${OLLMO_API}/system/status`);
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
  await fetch(`${OLLMO_API}/templates/save?name=${encodeURIComponent(name)}`, { method: "POST" });
  loadTemplates();
});

async function loadTemplates() {
  try {
    const r    = await fetch(`${OLLMO_API}/templates`);
    const list = await r.json();
    const el   = document.getElementById("template-list");
    el.innerHTML = "";
    list.forEach(name => {
      const btn     = document.createElement("button");
      btn.className = "mode-btn";
      btn.textContent = name;
      btn.onclick   = () =>
        fetch(`${OLLMO_API}/templates/${encodeURIComponent(name)}/load`, { method: "POST" })
          .then(poll);
      el.appendChild(btn);
    });
  } catch {}
}

// --- Init ---
fetchModesData().then(renderModeGrid);
fetchServicesCfg();
fetchPathsForRamTier();
loadTemplates();
connectStatusWS();
setInterval(pollStorage, 30000);
pollStorage();

// Immediate HTTP poll so gauges show values before first WS tick
poll();

// Timeline drag-to-resize handle
(function () {
  const container = document.querySelector('.tl-container');
  const handle    = document.getElementById('tl-drag-handle');
  if (!container || !handle) return;

  // Initial draw once layout is ready
  requestAnimationFrame(() => {
    if (timelineCanvas.offsetWidth > 0)
      Timeline.drawTimeline(timelineCanvas, null, activeViews);
  });

  let startY = 0, startH = 0;

  handle.addEventListener('mousedown', e => {
    startY = e.clientY;
    startH = container.offsetHeight;
    handle.classList.add('dragging');
    e.preventDefault();

    function onMove(e) {
      const newH = Math.max(60, Math.min(800, startH + (e.clientY - startY)));
      container.style.height = newH + 'px';
      Timeline.drawTimeline(timelineCanvas, null, activeViews);
    }

    function onUp() {
      handle.classList.remove('dragging');
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
})();

// Redraw canvas on browser resize
window.addEventListener('resize', () => {
  if (timelineCanvas.offsetWidth > 0)
    Timeline.drawTimeline(timelineCanvas, null, activeViews);
});
