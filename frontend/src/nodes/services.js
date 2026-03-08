/**
 * oAIo Service Nodes — Litegraph.js definitions
 * Routing-driven I/O ports derived from actual service connections.
 */

// Service → input/output port definitions based on routing architecture
const SERVICE_PORTS = {
  ollama:       { in: [["llm_req", "request"]],     out: [["llm_resp", "response"]] },
  "open-webui": { in: [["llm_resp", "response"], ["tts_audio", "audio"], ["image", "image"]],
                  out: [["llm_req", "request"], ["tts_req", "request"], ["imggen_req", "request"]] },
  "kokoro-tts": { in: [["tts_req", "request"]],     out: [["raw_audio", "audio"]] },
  rvc:          { in: [["raw_audio", "audio"], ["clone_audio", "audio"]],
                  out: [["tts_audio", "audio"]] },
  "f5-tts":     { in: [["clone_req", "request"]],   out: [["clone_audio", "audio"]] },
  comfyui:      { in: [["imggen_req", "request"]],   out: [["image", "image"]] },
  styletts2:    { in: [["tts_req", "request"]],      out: [["raw_audio", "audio"]] },
};

// Fallback for unknown services
const DEFAULT_PORTS = { in: [["input", "any"]], out: [["output", "any"]] };

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
      group:      svc.group || "Other",
      port:       svc.port  || 0,
      vram:       svc.vram_est_gb || 0,
      memoryMode: svc.memory_mode || "vram",
    }));
  } catch (e) {
    console.warn("[oAIo] Could not load service defs:", e);
  }

  defs.forEach(def => {
    const io = SERVICE_PORTS[def.name] || DEFAULT_PORTS;
    const shortLabel = def.name.charAt(0).toUpperCase() + def.name.slice(1);

    function NodeClass() {
      io.in.forEach(([name, type]) => this.addInput(name, type));
      io.out.forEach(([name, type]) => this.addOutput(name, type));
      this.title   = shortLabel;
      const _cs = getComputedStyle(document.documentElement);
      const _grpKey = {"oLLM":"--grp-llm","oAudio":"--grp-audio","Render":"--grp-render","Control":"--grp-control"}[def.group];
      const _grpHex = _grpKey ? (_cs.getPropertyValue(_grpKey).trim() || "#555") : "";
      this.color   = _cs.getPropertyValue("--bg3").trim() || "#161616";
      this.bgcolor = _grpHex ? `rgb(${parseInt(_grpHex.slice(1,3),16)*0.12|0},${parseInt(_grpHex.slice(3,5),16)*0.12|0},${parseInt(_grpHex.slice(5,7),16)*0.12|0})` : (_cs.getPropertyValue("--bg2").trim() || "#0f0f0f");
      this.size    = [180, 90];
      this._svc = {
        name:       def.name,
        group:      def.group,
        port:       def.port,
        vramEst:    def.vram,
        memoryMode: def.memoryMode,
        desc:       def.label,
        status:     "unknown",
        ramUsed:    0,
      };
      // Sparkline rolling buffer (30 samples, updated from WS 1Hz push)
      this._sparkData  = [];
      this._sparkMax   = 30;
      // Cache the full group hex color for sparkline rendering
      this._grpHex = _grpHex || "#555";
      this._sparkPhase = 0;        // animation phase for glow pulse
      this._sparkAnim  = null;     // requestAnimationFrame id
      this._refreshStatus();
    }

    NodeClass.prototype = Object.create(LGraphNode.prototype);

    NodeClass.prototype._refreshStatus = async function() {
      try {
        const r = await fetch(`${OLLMO_API}/services/${this._svc.name}/status`);
        const d = await r.json();
        this._svc.status  = d.status || "unknown";
        this._svc.ramUsed = d.ram_used_gb || 0;
      } catch { this._svc.status = "error"; }
      // Color title bar by status
      const _cs = getComputedStyle(document.documentElement);
      const _green = _cs.getPropertyValue("--tier-ram-bg").trim() || "#0a2a14";
      const _red = _cs.getPropertyValue("--tier-sata-bg").trim() || "#2a1e00";
      this.color = this._svc.status === "running" ? _green
                 : this._svc.status === "stopped" ? _red
                 : _cs.getPropertyValue("--tier-nvme-bg").trim() || "#0d1a2f";
      this.setDirtyCanvas(true);
    };

    NodeClass.prototype.onDrawForeground = function(ctx) {
      const s = this._svc;
      // Status bar under title
      const _cs = getComputedStyle(document.documentElement);
      const barColor = s.status === "running" ? (_cs.getPropertyValue("--green").trim() || "#00e676")
                     : s.status === "stopped" ? (_cs.getPropertyValue("--red").trim() || "#ff1744")
                     : (_cs.getPropertyValue("--yellow").trim() || "#ffd740");
      ctx.fillStyle = barColor;
      ctx.fillRect(0, 0, this.size[0], 2);

      // ── Sparkline ──────────────────────────────────────
      const buf = this._sparkData;
      if (!buf || buf.length < 2) return;

      const pad   = 6;
      const sparkH = 22;
      const sparkY = this.size[1] - sparkH - pad;
      const sparkW = this.size[0] - pad * 2;
      const sparkX = pad;

      // Normalize values 0..1
      let maxVal = 0;
      for (let i = 0; i < buf.length; i++) { if (buf[i] > maxVal) maxVal = buf[i]; }
      if (maxVal < 0.01) maxVal = 1;  // avoid div-by-zero for all-zero

      const stepX = sparkW / (this._sparkMax - 1);

      // Build path points
      const pts = [];
      for (let i = 0; i < buf.length; i++) {
        pts.push({
          x: sparkX + (this._sparkMax - buf.length + i) * stepX,
          y: sparkY + sparkH - (buf[i] / maxVal) * sparkH,
        });
      }

      // Filled area (subtle)
      const grpColor = this._grpHex;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(pts[0].x, sparkY + sparkH);
      for (let i = 0; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
      ctx.lineTo(pts[pts.length - 1].x, sparkY + sparkH);
      ctx.closePath();
      ctx.fillStyle = grpColor + "18";  // ~9% opacity hex suffix
      ctx.fill();

      // Sparkline stroke
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
      ctx.strokeStyle = grpColor + "cc";  // 80% opacity
      ctx.lineWidth   = 1.5;
      ctx.lineJoin    = "round";
      ctx.stroke();

      // Glow dot on latest value (pulsing when animations enabled)
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
        // Outer glow ring
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

    // Push a new sparkline sample (called from syncNodeStatuses)
    NodeClass.prototype._sparkPush = function(value) {
      this._sparkData.push(value);
      if (this._sparkData.length > this._sparkMax) this._sparkData.shift();
      // Start glow animation if not running
      if (!this._sparkAnim) this._sparkStartAnim();
    };

    // Smooth glow pulse animation (throttled to ~20fps to stay lightweight)
    NodeClass.prototype._sparkStartAnim = function() {
      const self = this;
      let last = performance.now();
      const interval = 50;  // ms between frames (~20fps)
      function tick(now) {
        self._sparkAnim = requestAnimationFrame(tick);
        if (now - last < interval) return;  // throttle
        // Skip redraw if CONFIG tab is not visible
        const configTab = document.getElementById("tab-config");
        if (configTab && !configTab.classList.contains("active")) return;
        const dt = (now - last) / 1000;
        last = now;
        self._sparkPhase = (self._sparkPhase || 0) + dt * 3.0;  // ~0.5Hz pulse
        if (self._sparkPhase > Math.PI * 200) self._sparkPhase -= Math.PI * 200;
        self.setDirtyCanvas(true);
      }
      this._sparkAnim = requestAnimationFrame(tick);
    };

    // Cleanup animation on node removal
    NodeClass.prototype.onRemoved = function() {
      if (this._sparkAnim) { cancelAnimationFrame(this._sparkAnim); this._sparkAnim = null; }
    };

    NodeClass.prototype.onMouseDown = function(e, pos) {
      // Click title bar to toggle start/stop
      if (pos[1] < 0) {
        const action = this._svc.status === "running" ? "stop" : "start";
        fetch(`${OLLMO_API}/services/${this._svc.name}/${action}`, { method: "POST" })
          .then(r => { if (!r.ok) throw new Error(`${r.status}`); return r.json(); })
          .then(d => { if (d.error) throw new Error(d.error); this._refreshStatus(); })
          .catch(err => {
            if (typeof showAlert === "function") showAlert("warning", `${this._svc.name} ${action} failed: ${err.message}`);
            this._refreshStatus();
          });
        return true;
      }
    };

    NodeClass.title = shortLabel;
    LiteGraph.registerNodeType(`oAIo/${def.name}`, NodeClass);
  });
}

registerServiceNodes();
