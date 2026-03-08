/**
 * Fleet extension — LiteGraph node types
 * Registered by extensions-loader.js when the fleet extension is enabled.
 */

// ── FleetNode — represents a remote oAIo instance ────────────────────────────

(function () {
  // CSS custom-property reader with fallback (mirrors capabilities.js pattern)
  function _cv(name, fb) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fb;
  }

  function FleetNode() {
    this.addOutput("status", "string");
    this.addOutput("vram",   "number");
    this.title   = "Fleet Node";
    this.color   = _cv("--bg3", "#161616");
    this.bgcolor = _cv("--bg2", "#0f0f0f");
    this.size    = [220, 110];
    this.resizable = true;

    this._nodeId   = null;   // fleet node id (8-char UUID prefix)
    this._name     = "—";
    this._url      = "";
    this._reachable = false;
    this._vram     = null;
    this._services = {};
    this._polling  = null;

    this._startPolling();
  }

  FleetNode.prototype = Object.create(LGraphNode.prototype);
  FleetNode.title = "Fleet Node";

  FleetNode.prototype._startPolling = function () {
    if (this._polling) return;
    this._polling = setInterval(() => this._refresh(), 5000);
    this._refresh();
  };

  FleetNode.prototype._refresh = async function () {
    if (!this._nodeId) return;
    try {
      const r = await fetch(`${OLLMO_API}/extensions/fleet/nodes/${this._nodeId}`);
      if (!r.ok) return;
      const d = await r.json();
      this._reachable = d.reachable ?? false;
      this._name      = d.name || this._name;
      this._vram      = d.live?.vram || null;
      this._services  = d.capabilities || {};
      this.title      = this._name;
      this.setOutputData(0, this._reachable ? "online" : "offline");
      this.setOutputData(1, this._vram?.used_gb ?? 0);
      this.setDirtyCanvas(true);
    } catch {}
  };

  FleetNode.prototype.onRemoved = function () {
    if (this._polling) { clearInterval(this._polling); this._polling = null; }
  };

  FleetNode.prototype.onDrawBackground = function (ctx) {
    // Status dot
    const color = this._reachable ? _cv("--green", "#00e676") : _cv("--text-dim", "#555");
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(this.size[0] - 14, 14, 5, 0, Math.PI * 2);
    ctx.fill();
  };

  FleetNode.prototype.onDrawForeground = function (ctx) {
    ctx.fillStyle = _cv("--text-dim", "#555");
    ctx.font = "10px monospace";
    const url  = this._url  || "not configured";
    const vram = this._vram
      ? `${this._vram.used_gb} / ${this._vram.total_gb} GB`
      : "—";
    const svcs = Object.keys(this._services).length;
    ctx.fillText(`URL:  ${url.replace(/^https?:\/\//, "")}`, 8, this.size[1] - 56);
    ctx.fillText(`VRAM: ${vram}`,                             8, this.size[1] - 42);
    ctx.fillText(`Svcs: ${svcs}`,                             8, this.size[1] - 28);
    ctx.fillText(this._reachable ? "● ONLINE" : "○ OFFLINE",  8, this.size[1] - 14);
  };

  FleetNode.prototype.onMouseDown = function (e, pos) {
    // Top-right corner → ping node
    if (pos[0] > this.size[0] - 30 && pos[1] < 30 && this._nodeId) {
      fetch(`${OLLMO_API}/extensions/fleet/nodes/${this._nodeId}/ping`, { method: "POST" })
        .then(() => this._refresh());
      return true;
    }
  };

  // Properties panel integration — show URL input
  FleetNode.prototype.onSelected = function () {};

  FleetNode.prototype.configure = function (data) {
    LGraphNode.prototype.configure.call(this, data);
    if (data._nodeId) { this._nodeId = data._nodeId; this._startPolling(); }
    if (data._url)    { this._url    = data._url; }
    if (data._name)   { this._name   = data._name; this.title = data._name; }
  };

  FleetNode.prototype.serialize = function () {
    const data = LGraphNode.prototype.serialize.call(this);
    data._nodeId = this._nodeId;
    data._url    = this._url;
    data._name   = this._name;
    return data;
  };

  LiteGraph.registerNodeType("oAIo/fleet-node", FleetNode);


  // ── FleetJobNode — dispatch a job to a fleet node ──────────────────────────

  function FleetJobNode() {
    this.addInput("node",    "string");
    this.addOutput("result", "string");
    this.title   = "Fleet Job";
    this.color   = _cv("--bg3", "#161616");
    this.bgcolor = _cv("--bg2", "#0f0f0f");
    this.size    = [220, 90];
    this.resizable = true;

    this._type   = "mode_activate";
    this._target = "";
    this._status = "idle";
    this._result = null;

    this.addProperty("job_type", "mode_activate");
    this.addProperty("target",   "");
  }

  FleetJobNode.prototype = Object.create(LGraphNode.prototype);
  FleetJobNode.title = "Fleet Job";

  FleetJobNode.prototype.onDrawForeground = function (ctx) {
    ctx.fillStyle = this._status === "complete" ? _cv("--green", "#00e676")
                  : this._status === "failed"   ? _cv("--red", "#ff1744")
                  : _cv("--text-dim", "#555");
    ctx.font = "10px monospace";
    ctx.fillText(`Type:   ${this.properties.job_type}`, 8, this.size[1] - 42);
    ctx.fillText(`Target: ${this.properties.target || "—"}`, 8, this.size[1] - 28);
    ctx.fillText(`Status: ${this._status}`,                  8, this.size[1] - 14);
  };

  FleetJobNode.prototype.onMouseDown = function (e, pos) {
    if (pos[1] < 24) { this._dispatch(); return true; }
  };

  FleetJobNode.prototype._dispatch = async function () {
    const nodeInput = this.getInputData(0);
    if (!nodeInput) { this._status = "no node"; this.setDirtyCanvas(true); return; }

    this._status = "dispatching";
    this.setDirtyCanvas(true);

    try {
      const r = await fetch(`${OLLMO_API}/extensions/fleet/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          node_id: nodeInput,
          type:    this.properties.job_type,
          target:  this.properties.target,
        }),
      });
      const job = await r.json();
      this._status = job.status || "unknown";
      this._result = job.result;
      this.setOutputData(0, this._status);
    } catch (e) {
      this._status = "error";
    }
    this.setDirtyCanvas(true);
  };

  LiteGraph.registerNodeType("oAIo/fleet-job", FleetJobNode);

})();
