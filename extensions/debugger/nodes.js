/**
 * Debugger extension — LiteGraph node types
 * Registered by extensions-loader.js when the debugger extension is enabled.
 */

(function () {

  // ── DebuggerLogNode — live log stream via WebSocket ────────────────────────

  function DebuggerLogNode() {
    this.title   = "Debugger Log";
    this.color   = "#1a1a1a";
    this.bgcolor = "#111111";
    this.size    = [320, 200];
    this.resizable = true;

    this.addProperty("container", "oaio");

    this._ws       = null;
    this._lines    = [];   // [{text, level}]
    this._maxLines = 80;

    this._connectWS();
  }

  DebuggerLogNode.prototype = Object.create(LGraphNode.prototype);
  DebuggerLogNode.title = "Debugger Log";

  DebuggerLogNode.prototype._connectWS = function () {
    if (this._ws) {
      try { this._ws.close(); } catch (_) {}
      this._ws = null;
    }
    const containerName = (this.properties && this.properties.container) || "oaio";
    const wsBase = OLLMO_API.replace(/^http/, "ws");
    const url = `${wsBase}/extensions/debugger/ws/${containerName}`;

    try {
      const ws = new WebSocket(url);
      this._ws = ws;

      ws.onmessage = (evt) => {
        try {
          const obj = JSON.parse(evt.data);
          this._lines.push({ text: obj.line || "", level: obj.level || "info" });
          if (this._lines.length > this._maxLines) {
            this._lines.splice(0, this._lines.length - this._maxLines);
          }
          this.setDirtyCanvas(true);
        } catch (_) {}
      };

      ws.onerror = () => {
        this._lines.push({ text: "[debugger] WebSocket error", level: "error" });
        this.setDirtyCanvas(true);
      };

      ws.onclose = () => {
        this._lines.push({ text: "[debugger] connection closed", level: "warn" });
        this.setDirtyCanvas(true);
      };
    } catch (e) {
      this._lines.push({ text: `[debugger] failed to connect: ${e}`, level: "error" });
    }
  };

  DebuggerLogNode.prototype.onRemoved = function () {
    if (this._ws) {
      try { this._ws.close(); } catch (_) {}
      this._ws = null;
    }
  };

  DebuggerLogNode.prototype.configure = function (data) {
    LGraphNode.prototype.configure.call(this, data);
    // Reconnect with (potentially restored) container property
    this._lines = [];
    this._connectWS();
  };

  DebuggerLogNode.prototype.serialize = function () {
    const data = LGraphNode.prototype.serialize.call(this);
    return data;
  };

  DebuggerLogNode.prototype.onPropertyChanged = function (name, value) {
    if (name === "container") {
      this._lines = [];
      this._connectWS();
    }
  };

  DebuggerLogNode.prototype.onDrawForeground = function (ctx) {
    if (!this._lines.length) {
      ctx.fillStyle = "#555";
      ctx.font = "10px monospace";
      ctx.fillText("connecting...", 8, this.size[1] - 8);
      return;
    }

    ctx.font = "9px monospace";
    const lineH  = 11;
    const padX   = 6;
    const areaH  = this.size[1] - 20;
    const maxVis = Math.floor(areaH / lineH);
    const start  = Math.max(0, this._lines.length - maxVis);
    const vis    = this._lines.slice(start);

    // Clip to node body
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, this.size[0], this.size[1] - 4);
    ctx.clip();

    let y = this.size[1] - 8 - (vis.length - 1) * lineH;
    for (const entry of vis) {
      if (entry.level === "error") {
        ctx.fillStyle = "#f44336";
      } else if (entry.level === "warn") {
        ctx.fillStyle = "#ff9800";
      } else {
        ctx.fillStyle = "#888";
      }
      // Truncate long lines
      const maxChars = Math.floor((this.size[0] - padX * 2) / 5.5);
      const text = entry.text.length > maxChars
        ? entry.text.slice(0, maxChars - 1) + "…"
        : entry.text;
      ctx.fillText(text, padX, y);
      y += lineH;
    }

    ctx.restore();

    // Container label in top-left corner
    const cname = (this.properties && this.properties.container) || "oaio";
    ctx.fillStyle = "#444";
    ctx.font = "9px monospace";
    ctx.fillText(`[${cname}]`, padX, 12);
  };

  LiteGraph.registerNodeType("oAIo/debugger-log", DebuggerLogNode);


  // ── DebuggerErrorNode — polls /errors every 10s, shows count + last 3 ──────

  function DebuggerErrorNode() {
    this.addOutput("error_count", "number");
    this.title   = "Debugger Errors";
    this.color   = "#2e1a1a";
    this.bgcolor = "#221212";
    this.size    = [280, 120];
    this.resizable = true;

    this.addProperty("container", "oaio");

    this._count    = 0;
    this._last3    = [];
    this._polling  = null;

    this._startPolling();
  }

  DebuggerErrorNode.prototype = Object.create(LGraphNode.prototype);
  DebuggerErrorNode.title = "Debugger Errors";

  DebuggerErrorNode.prototype._startPolling = function () {
    if (this._polling) return;
    this._fetch();
    this._polling = setInterval(() => this._fetch(), 10000);
  };

  DebuggerErrorNode.prototype._fetch = async function () {
    const cname = (this.properties && this.properties.container) || "oaio";
    try {
      const r = await fetch(`${OLLMO_API}/extensions/debugger/errors/${cname}?lines=100`);
      if (!r.ok) return;
      const d = await r.json();
      this._count = d.count || 0;
      this._last3 = (d.lines || []).slice(-3);
      this.setOutputData(0, this._count);
      this.setDirtyCanvas(true);
    } catch (_) {}
  };

  DebuggerErrorNode.prototype.onRemoved = function () {
    if (this._polling) { clearInterval(this._polling); this._polling = null; }
  };

  DebuggerErrorNode.prototype.configure = function (data) {
    LGraphNode.prototype.configure.call(this, data);
    this._startPolling();
  };

  DebuggerErrorNode.prototype.serialize = function () {
    return LGraphNode.prototype.serialize.call(this);
  };

  DebuggerErrorNode.prototype.onPropertyChanged = function (name, value) {
    if (name === "container") {
      this._count = 0;
      this._last3 = [];
      this._fetch();
    }
  };

  DebuggerErrorNode.prototype.onDrawForeground = function (ctx) {
    const cname = (this.properties && this.properties.container) || "oaio";
    ctx.font = "10px monospace";

    // Error count header
    ctx.fillStyle = this._count > 0 ? "#f44336" : "#4caf50";
    ctx.fillText(`[${cname}]  errors: ${this._count}`, 8, 16);

    // Last 3 error lines
    ctx.font = "9px monospace";
    ctx.fillStyle = "#f44336";
    const maxChars = Math.floor((this.size[0] - 16) / 5.5);
    let y = 34;
    for (const line of this._last3) {
      const text = line.length > maxChars ? line.slice(0, maxChars - 1) + "…" : line;
      ctx.fillText(text, 8, y);
      y += 13;
    }

    if (!this._last3.length) {
      ctx.fillStyle = "#555";
      ctx.fillText("no errors found", 8, 34);
    }
  };

  LiteGraph.registerNodeType("oAIo/debugger-errors", DebuggerErrorNode);

})();
