/**
 * oAIo Service Nodes — LiteGraph definitions
 * Graph-engine-driven: ports derived from auto-discovery API.
 * Falls back to static SERVICE_PORTS if graph API unavailable.
 */

// Static fallback ports (used if /graph/discover is unavailable)
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

const DEFAULT_PORTS = { in: [["input", "any"]], out: [["output", "any"]] };

// Graph-discovered port data — populated by registerGraphNodes()
let _graphPorts = {};

// Map graph data_type to LiteGraph slot type
const _GRAPH_TYPE_MAP = {
  "text": "string",
  "audio": "audio",
  "image": "image",
  "embedding": "array",
  "json": "object",
  "file-path": "string",
  "number": "number",
  "video": "video",
  "any": "any",
};

function _graphTypeToLG(dataType) {
  return _GRAPH_TYPE_MAP[dataType] || dataType || "any";
}

// Register service nodes from graph API, with fallback to static ports
async function registerServiceNodes() {
  // Try graph discovery first
  let graphNodes = null;
  try {
    const r = await fetch(`${OLLMO_API}/graph/discover`, { method: "POST" });
    if (r.ok) {
      const data = await r.json();
      graphNodes = data.nodes || {};
    }
  } catch {}

  // Load service definitions for metadata
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
      ram:        svc.ram_est_gb || 0,
      memoryMode: svc.memory_mode || "vram",
    }));
  } catch (e) {
    console.warn("[oAIo] Could not load service defs:", e);
  }

  defs.forEach(def => {
    let io;

    // Priority 1: graph-discovered ports
    if (graphNodes && graphNodes[def.name]) {
      const gNode = graphNodes[def.name];
      const inPorts = [];
      const outPorts = [];
      for (const plugin of (gNode.plugins || [])) {
        for (const port of (plugin.ports || [])) {
          const lgType = _graphTypeToLG(port.data_type);
          if (port.direction === "in") {
            inPorts.push([port.name, lgType]);
          } else {
            outPorts.push([port.name, lgType]);
          }
        }
      }
      io = { in: inPorts, out: outPorts };
      _graphPorts[def.name] = { node: gNode, io };
    }
    // Priority 2: static fallback
    else {
      io = SERVICE_PORTS[def.name] || DEFAULT_PORTS;
    }

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
      this.size    = [180, 70];
      this._svc = {
        name:       def.name,
        group:      def.group,
        port:       def.port,
        vramEst:    def.vram,
        ramEst:     def.ram,
        memoryMode: def.memoryMode,
        desc:       def.label,
        status:     "unknown",
        ramUsed:    0,
      };
      // Store graph node data for edge management
      if (_graphPorts[def.name]) {
        this._graphNode = _graphPorts[def.name].node;
      }
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
      const _cs = getComputedStyle(document.documentElement);
      const barColor = s.status === "running" ? (_cs.getPropertyValue("--green").trim() || "#00e676")
                     : s.status === "stopped" ? (_cs.getPropertyValue("--red").trim() || "#ff1744")
                     : (_cs.getPropertyValue("--yellow").trim() || "#ffd740");
      ctx.fillStyle = barColor;
      ctx.fillRect(0, 0, this.size[0], 2);
    };

    NodeClass.prototype.onMouseDown = function(e, pos) {
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
