/**
 * oAIo Service Nodes — Litegraph.js definitions
 * One node per service. Registers with LiteGraph.
 */

function colorFromPercent(pct) {
  if (pct < 0.6) return "#4caf50";
  if (pct < 0.85) return "#ffb300";
  return "#f44336";
}

class ServiceNode {
  constructor(name, group, port, vramEst) {
    this.name        = name;
    this.group       = group;
    this.port        = port;
    this.vramEst     = vramEst;
    this.status      = "unknown";
    this.ramUsed     = 0;
    this.size        = [200, 100];
    this.resizable   = true;
  }

  onDrawBackground(ctx) {
    const color = this.status === "running" ? "#4caf50"
                : this.status === "stopped" ? "#666"
                : "#ffb300";
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(this.size[0] - 14, 14, 5, 0, Math.PI * 2);
    ctx.fill();
  }

  onDrawForeground(ctx) {
    ctx.fillStyle = "#888";
    ctx.font = "10px monospace";
    ctx.fillText(`VRAM est: ${this.vramEst}GB`, 8, this.size[1] - 28);
    ctx.fillText(`RAM: ${this.ramUsed}GB`, 8, this.size[1] - 14);
    ctx.fillText(`Group: ${this.group}`, 8, this.size[1] - 42);
  }

  async refreshStatus() {
    try {
      const r = await fetch(`${OLLMO_API}/services/${this.name}/status`);
      const d = await r.json();
      this.status  = d.status || "unknown";
      this.ramUsed = d.ram_used_gb || 0;
    } catch { this.status = "error"; }
    this.setDirtyCanvas(true);
  }

  async toggle() {
    const action = this.status === "running" ? "stop" : "start";
    await fetch(`${OLLMO_API}/services/${this.name}/${action}`, { method: "POST" });
    await this.refreshStatus();
  }

  onMouseDown(e, pos) {
    if (pos[0] > this.size[0] - 30 && pos[1] < 30) {
      this.toggle();
      return true;
    }
  }
}

// Register service nodes from API — data-driven
async function registerServiceNodes() {
  let defs = [];
  try {
    const r = await fetch(`${OLLMO_API}/config/services`);
    const data = await r.json();
    defs = Object.entries(data).map(([name, svc]) => ({
      name,
      label: svc.description
        ? `${name.charAt(0).toUpperCase() + name.slice(1)} — ${svc.description}`
        : name,
      group: svc.group || "Other",
      port:  svc.port  || 0,
      vram:  svc.vram_est_gb || 0,
    }));
  } catch (e) {
    console.warn("[oAIo] Could not load service defs:", e);
  }

  defs.forEach(def => {
    function NodeClass() {
      this.addOutput("audio", "audio");
      this.addOutput("status", "string");
      this.title   = def.label;
      this.color   = "#1e1e1e";
      this.bgcolor = "#141414";
      this._svc    = new ServiceNode(def.name, def.group, def.port, def.vram);
      this._svc.refreshStatus();
    }
    NodeClass.prototype = Object.create(LGraphNode.prototype);
    NodeClass.prototype.onDrawBackground = function(ctx) { this._svc.onDrawBackground.call(this, ctx); };
    NodeClass.prototype.onDrawForeground = function(ctx) { this._svc.onDrawForeground.call(this, ctx); };
    NodeClass.prototype.onMouseDown      = function(e, p) { return this._svc.onMouseDown.call(this, e, p); };
    NodeClass.title = def.label;

    LiteGraph.registerNodeType(`oAIo/${def.name}`, NodeClass);
  });
}

registerServiceNodes();
