/**
 * oAIo — main app
 */

const OLLMO_API = window.location.origin;
const POLL_MS   = 3000;

// --- Litegraph setup ---
const graph  = new LGraph();
const canvas = new LGraphCanvas("#node-canvas", graph);

canvas.background_image = null;
canvas.render_shadows   = false;
canvas.show_info        = false;

const defaults = [
  ["oAIo/ollama",     [60,  60]],
  ["oAIo/kokoro-tts", [60,  200]],
  ["oAIo/rvc",        [300, 200]],
  ["oAIo/open-webui", [540, 60]],
  ["oAIo/f5-tts",     [60,  340]],
  ["oAIo/comfyui",    [540, 200]],
];

defaults.forEach(([type, pos]) => {
  const node = LiteGraph.createNode(type);
  if (node) { node.pos = pos; graph.add(node); }
});

graph.start();

// --- Timeline canvas ---
const timelineCanvas = document.getElementById("timeline-canvas");

// --- Gauges ---
function setGauge(barId, labelId, used, total) {
  const pct = Math.min(used / total, 1);
  const bar = document.getElementById(barId);
  bar.style.width = (pct * 100) + "%";
  bar.className = "fill" + (pct > 0.85 ? " hot" : pct > 0.6 ? " warn" : "");
  document.getElementById(labelId).textContent = `${used.toFixed(1)} / ${total} GB`;
}

// --- Timeline view toggles ---
const activeViews = new Set(["vram", "ram", "gpu"]);

// Pills that control multiple timeline rows
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
      // Only deactivate if it won't empty the set
      if (activeViews.size > rows.length) {
        rows.forEach(r => activeViews.delete(r));
        pill.classList.remove("active");
      }
    } else {
      rows.forEach(r => activeViews.add(r));
      pill.classList.add("active");
    }
    Timeline.drawTimeline(timelineCanvas, null, activeViews);
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
  graph.getNodes().forEach(node => {
    const g   = node._svc?.group;
    const dim = activeGroup !== "all" && g !== activeGroup;
    node.color   = dim ? "#222"    : "#1e1e1e";
    node.bgcolor = dim ? "#1a1a1a" : "#141414";
  });
  graph.setDirtyCanvas(true, true);
}

// --- Dual mode system ---
let activeModes = [];  // up to 2 mode IDs
let modesData   = {};  // fetched from API

async function fetchModesData() {
  try {
    const r = await fetch(`${OLLMO_API}/modes`);
    modesData = await r.json();
  } catch {}
}

function combinedVram() {
  return activeModes.reduce((s, id) => s + (modesData[id]?.vram_est_gb || 0), 0);
}

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

function updateModeButtons() {
  document.querySelectorAll(".mode-btn").forEach(btn => {
    const idx = activeModes.indexOf(btn.dataset.mode);
    btn.classList.toggle("active",           idx === 0);
    btn.classList.toggle("secondary-active", idx === 1);
  });
}

function deactivateMode(modeId) {
  activeModes = activeModes.filter(m => m !== modeId);
  renderModeTabs();
  updateModeButtons();
  if (selectedNode) loadModeAllocations(selectedNode);
}

document.querySelectorAll(".mode-btn").forEach(btn => {
  btn.addEventListener("click", async () => {
    const modeId = btn.dataset.mode;

    if (activeModes.includes(modeId)) {
      deactivateMode(modeId);
      return;
    }

    // Pre-flight VRAM check
    let projection = null;
    try {
      const chk = await fetch(`${OLLMO_API}/modes/${modeId}/check`);
      projection = await chk.json();
    } catch {}

    if (projection?.blocked) {
      showAlert("critical",
        `Cannot activate ${modeId.toUpperCase()}: ~${projection.projected_gb}GB projected exceeds VRAM limit.`
      );
      return;
    }

    if (activeModes.length >= 2) {
      activeModes[1] = modeId;
    } else {
      activeModes.push(modeId);
    }

    renderModeTabs();
    updateModeButtons();
    await fetchModesData();
    loadModeConfig(modeId);

    const r    = await fetch(`${OLLMO_API}/modes/${modeId}/activate`, { method: "POST" });
    const data = await r.json();
    if (data.warning) {
      showAlert("warning",
        `${modeId.toUpperCase()} active — ~${data.projection?.projected_gb}GB projected, approaching VRAM limit.`
      );
    }
    poll();
  });
});

// --- Sub-graph navigation (Tier 3 capability nodes) ---
const subGraphCache = {};
const navStack      = [];

async function enterContainer(node) {
  const svcName = node._svc?.name;
  if (!svcName) return;

  // Cache or build the sub-graph
  if (!subGraphCache[svcName]) {
    subGraphCache[svcName] = await CapabilityNodes.buildSubGraph(svcName);
  }

  navStack.push({ graph: canvas.graph, title: "ALL SERVICES" });

  canvas.graph.stop();
  canvas.graph = subGraphCache[svcName];
  canvas.graph.start();
  canvas.draw(true, true);

  // Show breadcrumb, hide group filter
  document.getElementById("group-filter").classList.add("hidden");
  document.getElementById("breadcrumb").classList.remove("hidden");
  document.getElementById("crumb-path").textContent =
    node.title || svcName.toUpperCase();
}

function exitContainer() {
  if (navStack.length === 0) return;
  const prev = navStack.pop();
  canvas.graph.stop();
  canvas.graph = prev.graph;
  canvas.graph.start();
  canvas.draw(true, true);

  if (navStack.length === 0) {
    document.getElementById("group-filter").classList.remove("hidden");
    document.getElementById("breadcrumb").classList.add("hidden");
  } else {
    document.getElementById("crumb-path").textContent =
      navStack[navStack.length - 1].title;
  }
}

document.getElementById("crumb-back").addEventListener("click", exitContainer);

// Double-click a container node → enter its sub-graph
canvas.onNodeDblClick = node => {
  if (node && node._svc) enterContainer(node);
};

// --- Config panel scope switching ---
function showConfigView(view) {
  // view = "node" | "mode" | "paths"
  document.getElementById("config-node").classList.toggle("hidden",  view !== "node");
  document.getElementById("config-mode").classList.toggle("hidden",  view !== "mode");
  document.getElementById("config-paths").classList.toggle("hidden", view !== "paths");
}

// --- Per-node config (persisted to localStorage) ---
let selectedNode = null;
const nodeConfigs = JSON.parse(localStorage.getItem("oaio-nodeConfigs") || "{}");

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
  localStorage.setItem("oaio-nodeConfigs", JSON.stringify(nodeConfigs));
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

  if (!svcName || activeModes.length === 0) {
    wrap.classList.add("hidden");
    return;
  }

  wrap.classList.remove("hidden");
  body.innerHTML = "";

  for (const modeId of activeModes) {
    const modeInfo = modesData[modeId] || {};
    let allocs = {};
    let budget = modeInfo.vram_est_gb || 20;
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
    const proj   = row.querySelector(".alloc-projection");

    slider.addEventListener("input", () => {
      label.textContent = slider.value + " GB";
    });
    slider.addEventListener("change", async () => {
      const r = await fetch(
        `${OLLMO_API}/modes/${modeId}/allocations/${encodeURIComponent(svcName)}`,
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ gb: parseFloat(slider.value) }) }
      );
      const data = await r.json();
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
  "", // 0 unused
  "Protected — last to close",
  "High — yields only to protected",
  "Normal — balanced scheduling",
  "Low — yields to higher priority",
  "Hot swap — first to park",
];

function updatePrioritySub() {
  const val = parseInt(document.getElementById("cfg-priority").value) || 1;
  setSubHtml("sub-priority",
    `<span class="sub-desc">${val} — ${PRIORITY_DESC[val] || ""}</span>`
  );
}

const BUS_DESC = {
  nvme:  "Est. load: ~1.5 s",
  sata:  "Est. load: ~13 s",
  ram:   "Est. load: ~0.1 s",
};

function updateBusSub() {
  const val = document.getElementById("cfg-bus").value;
  setSubHtml("sub-bus",
    `<span class="sub-desc">${BUS_DESC[val] || ""}</span>`
  );
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

// --- Mode-level config (total system allocation) ---
const modeConfigs = JSON.parse(localStorage.getItem("oaio-modeConfigs") || "{}");
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

  const svcs = info.services || [];
  document.getElementById("mode-services").textContent = svcs.join(", ") || "—";

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
  localStorage.setItem("oaio-modeConfigs", JSON.stringify(modeConfigs));
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

// Mode config slider live update
document.getElementById("mode-vram").addEventListener("input", e => {
  document.getElementById("mode-vram-val").textContent = e.target.value + " GB";
  saveModeConfig();
});
document.getElementById("mode-vram").addEventListener("change", async e => {
  if (!selectedModeId) return;
  await fetch(`${OLLMO_API}/modes/${selectedModeId}/budget`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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

// --- Paths & Routing panel ---
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
    document.getElementById("route-tts-url").value    = routing.tts_url    || "";
    document.getElementById("route-imggen-url").value = routing.image_gen_url || "";
    document.getElementById("route-ollama-url").value = routing.ollama_url  || "";
    document.getElementById("route-stt-url").value    = routing.stt_url     || "";
  } catch (e) {
    console.warn("loadPathsPanel error:", e);
  }
}

function renderPathsList(paths) {
  const list = document.getElementById("paths-list");
  list.innerHTML = "";
  paths.forEach(p => {
    const entry = document.createElement("div");
    entry.className = "path-entry";
    entry.dataset.name = p.name;
    entry.innerHTML =
      `<span class="path-entry-label">${p.label}</span>` +
      `<span class="path-entry-target" title="${p.target || 'missing'}">${p.target || '—'}</span>` +
      `<span class="tier-badge ${p.tier}">${p.tier}</span>` +
      `<button class="path-edit-btn" data-name="${p.name}">EDIT</button>`;
    entry.querySelector(".path-edit-btn").addEventListener("click", () =>
      startPathEdit(entry, p.name, p.target || p.default_target)
    );
    list.appendChild(entry);
  });
}

function startPathEdit(entry, name, currentTarget) {
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
    if (!newTarget) { loadPathsPanel(); return; }
    try {
      await fetch(`${OLLMO_API}/config/paths/${encodeURIComponent(name)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: newTarget }),
      });
    } catch (e) {
      console.warn("repoint error:", e);
    }
    loadPathsPanel();
  };

  editBtn.onclick = save;
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") save();
    if (e.key === "Escape") loadPathsPanel();
  });
}

document.getElementById("config-paths-btn").addEventListener("click", loadPathsPanel);

document.getElementById("save-routing-btn").addEventListener("click", async () => {
  const body = {
    tts_url:       document.getElementById("route-tts-url").value.trim(),
    image_gen_url: document.getElementById("route-imggen-url").value.trim(),
    ollama_url:    document.getElementById("route-ollama-url").value.trim(),
    stt_url:       document.getElementById("route-stt-url").value.trim(),
  };
  try {
    await fetch(`${OLLMO_API}/config/routing`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    console.warn("save routing error:", e);
  }
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
  // Only surface the highest-severity live alert
  const top = alerts.find(a => a.level === "critical") || alerts[0];
  showAlert(top.level, top.message);
}

// --- Poll system status ---
async function poll() {
  try {
    const [statusR, storageR] = await Promise.all([
      fetch(`${OLLMO_API}/system/status`),
      fetch(`${OLLMO_API}/config/storage/stats`).catch(() => null),
    ]);
    const d = await statusR.json();
    const storage = storageR ? await storageR.json() : null;

    setGauge("vram-bar", "vram-label", d.vram?.used_gb || 0, d.vram?.total_gb || 20);
    setGauge("ram-bar",  "ram-label",  d.ram?.used_gb  || 0, d.ram?.total_gb  || 62);

    const gpuPct = (d.gpu?.gpu_use_percent || 0) / 100;
    const gpuBar = document.getElementById("gpu-bar");
    gpuBar.style.width = (gpuPct * 100) + "%";
    gpuBar.className = "fill" + (gpuPct > 0.85 ? " hot" : gpuPct > 0.6 ? " warn" : "");
    document.getElementById("gpu-label").textContent = `${d.gpu?.gpu_use_percent || 0}%`;

    if (storage) d.storage = storage;
    syncAlerts(d.alerts);
    Timeline.drawTimeline(timelineCanvas, d, activeViews);
  } catch (e) {
    console.warn("oLLMo API not reachable:", e.message);
  }
}

// --- Save template ---
document.getElementById("save-template").addEventListener("click", async () => {
  const name = prompt("Template name:");
  if (!name) return;
  await fetch(`${OLLMO_API}/templates/save?name=${encodeURIComponent(name)}`, {
    method: "POST"
  });
  loadTemplates();
});

// --- Load templates ---
async function loadTemplates() {
  try {
    const r    = await fetch(`${OLLMO_API}/templates`);
    const list = await r.json();
    const el   = document.getElementById("template-list");
    el.innerHTML = "";
    list.forEach(name => {
      const btn       = document.createElement("button");
      btn.className   = "mode-btn";
      btn.textContent = name;
      btn.onclick     = () =>
        fetch(`${OLLMO_API}/templates/${encodeURIComponent(name)}/load`, { method: "POST" })
          .then(poll);
      el.appendChild(btn);
    });
  } catch {}
}

// --- Init ---
fetchModesData();
loadTemplates();
setInterval(poll, POLL_MS);
poll();
