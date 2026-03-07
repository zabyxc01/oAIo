/**
 * oAIo Capability Nodes — Tier 3 sub-nodes
 * Appear inside container node sub-graphs:
 *   oAIo/llm-model   — inside Ollama
 *   oAIo/workflow    — inside ComfyUI
 *   oAIo/voice-model — inside RVC
 *   oAIo/tts-voice   — inside Kokoro
 */

// OLLMO_API defined globally in index.html

// ── Workflow tag detection ───────────────────────────────────────────────────
function _cv(name, fb) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fb;
}
function tagWorkflow(name) {
  const n = name.toLowerCase();
  if (/mvadapter|mv.adapter|mv_adapter|multiview|t2mv/.test(n))
    return { tag: "3D", color: _cv("--cyan", "#00d2be") };
  if (/3d|tpose|wireframe|zero123|triposr/.test(n))
    return { tag: "3D", color: _cv("--cyan", "#00d2be") };
  if (/video|animate|motion|wan|ltx/.test(n))
    return { tag: "VIDEO", color: _cv("--purple", "#a855f7") };
  if (/pose|control/.test(n))
    return { tag: "POSE", color: _cv("--tier-sata", "#ffa726") };
  if (/upscale|esrgan/.test(n))
    return { tag: "UPSCALE", color: _cv("--grp-control", "#78909c") };
  if (/flux|lora|sdxl|sd15|image/.test(n))
    return { tag: "IMAGE", color: _cv("--green", "#00e676") };
  return { tag: "CUSTOM", color: _cv("--text-dim", "#555") };
}

// ── LLM Model node (inside Ollama) ───────────────────────────────────────────
(function () {
  function LLMModelNode() {
    this.title    = "model";
    this.size     = [200, 90];
    this._name    = "";
    this._sizeGb  = 0;
    this._loaded  = false;
    this.color    = _cv("--bg3", "#161616");
    this.bgcolor  = _cv("--bg2", "#0f0f0f");
  }

  LLMModelNode.prototype.onDrawBackground = function (ctx) {
    const dot = this._loaded ? _cv("--green", "#00e676") : _cv("--border", "#252525");
    ctx.fillStyle = dot;
    ctx.beginPath();
    ctx.arc(this.size[0] - 14, 14, 5, 0, Math.PI * 2);
    ctx.fill();
  };

  LLMModelNode.prototype.onDrawForeground = function (ctx) {
    ctx.font = "10px monospace";
    ctx.fillStyle = this._loaded ? _cv("--green", "#00e676") : _cv("--text-dim", "#555");
    ctx.fillText(this._loaded ? "LOADED" : "unloaded", 8, this.size[1] - 28);
    ctx.fillStyle = _cv("--text-dim", "#555");
    ctx.fillText(`~${this._sizeGb}GB`, 8, this.size[1] - 14);
  };

  LLMModelNode.prototype.onMouseDown = function (e, pos) {
    if (pos[0] > this.size[0] - 30 && pos[1] < 30) {
      this._load();
      return true;
    }
  };

  LLMModelNode.prototype._load = async function () {
    await fetch(`${OLLMO_API}/services/ollama/models/${encodeURIComponent(this._name)}/load`,
      { method: "POST" });
    this._loaded = true;
    this.setDirtyCanvas(true);
  };

  LLMModelNode.title = "LLM Model";
  LiteGraph.registerNodeType("oAIo/llm-model", LLMModelNode);
})();

// ── Workflow node (inside ComfyUI) ───────────────────────────────────────────
(function () {
  function WorkflowNode() {
    this.title   = "workflow";
    this.size    = [210, 90];
    this._file   = "";
    this._tag    = "CUSTOM";
    this._color  = _cv("--text-dim", "#555");
    this.color   = _cv("--bg3", "#161616");
    this.bgcolor = _cv("--bg2", "#0f0f0f");
  }

  WorkflowNode.prototype.onDrawBackground = function (ctx) {
    ctx.fillStyle = this._color;
    ctx.fillRect(0, 0, this.size[0], 3);
  };

  WorkflowNode.prototype.onDrawForeground = function (ctx) {
    ctx.fillStyle = this._color;
    ctx.font = "bold 9px monospace";
    ctx.fillText(`[${this._tag}]`, 8, this.size[1] - 14);
    ctx.fillStyle = _cv("--border", "#252525");
    ctx.font = "9px monospace";
    ctx.fillText(this._file, 8, this.size[1] - 26);
  };

  WorkflowNode.title = "Workflow";
  LiteGraph.registerNodeType("oAIo/workflow", WorkflowNode);
})();

// ── Voice Model node (inside RVC) ─────────────────────────────────────────────
(function () {
  function VoiceModelNode() {
    this.title   = "voice";
    this.size    = [180, 80];
    this._file   = "";
    this._active = false;
    this.color   = _cv("--bg3", "#161616");
    this.bgcolor = _cv("--bg2", "#0f0f0f");
  }

  VoiceModelNode.prototype.onDrawBackground = function (ctx) {
    const dot = this._active ? _cv("--yellow", "#ffd740") : _cv("--border", "#252525");
    ctx.fillStyle = dot;
    ctx.beginPath();
    ctx.arc(this.size[0] - 14, 14, 5, 0, Math.PI * 2);
    ctx.fill();
  };

  VoiceModelNode.prototype.onDrawForeground = function (ctx) {
    ctx.font = "10px monospace";
    ctx.fillStyle = this._active ? _cv("--yellow", "#ffd740") : _cv("--text-dim", "#555");
    ctx.fillText(this._active ? "ACTIVE" : "inactive", 8, this.size[1] - 14);
  };

  VoiceModelNode.prototype.onMouseDown = function (e, pos) {
    if (pos[0] > this.size[0] - 30 && pos[1] < 30) {
      this._activate();
      return true;
    }
  };

  VoiceModelNode.prototype._activate = async function () {
    await fetch(`${OLLMO_API}/services/rvc/models/${encodeURIComponent(this._file)}/activate`,
      { method: "POST" });
    // mark active, deactivate siblings — sub-graph refresh handles this
    window._rvcActiveModel = this._file;
    this._active = true;
    this.setDirtyCanvas(true);
  };

  VoiceModelNode.title = "Voice Model";
  LiteGraph.registerNodeType("oAIo/voice-model", VoiceModelNode);
})();

// ── TTS Voice alias node (inside Kokoro) ──────────────────────────────────────
(function () {
  function TTSVoiceNode() {
    this.title   = "tts-voice";
    this.size    = [160, 70];
    this._alias  = "";
    this._mapped = "";
    this.color   = _cv("--bg3", "#161616");
    this.bgcolor = _cv("--bg2", "#0f0f0f");
  }

  TTSVoiceNode.prototype.onDrawForeground = function (ctx) {
    ctx.font = "9px monospace";
    ctx.fillStyle = _cv("--text-dim", "#555");
    ctx.fillText(`→ ${this._mapped}`, 8, this.size[1] - 14);
  };

  TTSVoiceNode.title = "TTS Voice";
  LiteGraph.registerNodeType("oAIo/tts-voice", TTSVoiceNode);
})();

// ── Sub-graph builder ────────────────────────────────────────────────────────
window.CapabilityNodes = {
  tagWorkflow,

  async buildSubGraph(svcName) {
    const g = new LGraph();

    if (svcName === "ollama") {
      const models = await fetch(`${OLLMO_API}/services/ollama/models`)
        .then(r => r.json()).catch(() => []);
      if (!models.error) {
        models.forEach((m, i) => {
          const node = LiteGraph.createNode("oAIo/llm-model");
          node.title   = m.name;
          node._name   = m.name;
          node._sizeGb = m.size_gb;
          node._loaded = false;
          node.pos = [60 + (i % 3) * 230, 60 + Math.floor(i / 3) * 130];
          g.add(node);
        });
      }

    } else if (svcName === "comfyui") {
      const workflows = await fetch(`${OLLMO_API}/services/comfyui/workflows`)
        .then(r => r.json()).catch(() => []);
      workflows.forEach((w, i) => {
        const node   = LiteGraph.createNode("oAIo/workflow");
        const tagged = tagWorkflow(w.name);
        node.title   = w.name;
        node._file   = w.file;
        node._tag    = tagged.tag;
        node._color  = tagged.color;
        node.pos = [60 + (i % 3) * 240, 60 + Math.floor(i / 3) * 130];
        g.add(node);
      });

    } else if (svcName === "rvc") {
      const voices = await fetch(`${OLLMO_API}/services/rvc/models`)
        .then(r => r.json()).catch(() => []);
      if (!voices.error) {
        voices.forEach((v, i) => {
          const node    = LiteGraph.createNode("oAIo/voice-model");
          node.title    = v.name;
          node._file    = v.file;
          node._active  = false;
          node.pos = [60 + i * 210, 80];
          g.add(node);
        });
      }

    } else if (svcName === "kokoro-tts") {
      const VOICE_MAP = {
        alloy: "af_heart", nova: "af_heart",
        shimmer: "af_sky", echo: "af_sky",
        fable: "bf_emma", onyx: "am_adam"
      };
      Object.entries(VOICE_MAP).forEach(([alias, mapped], i) => {
        const node    = LiteGraph.createNode("oAIo/tts-voice");
        node.title    = alias;
        node._alias   = alias;
        node._mapped  = mapped;
        node.pos = [60 + (i % 3) * 190, 60 + Math.floor(i / 3) * 110];
        g.add(node);
      });
    }

    g.start();
    return g;
  },

  // Highlight capability nodes matching a mode's service list
  applyModeHighlight(graph, activeServices) {
    if (!graph) return;
    (graph._nodes || []).forEach(node => {
      const inMode = activeServices.some(s => node.title?.toLowerCase().includes(s));
      node.color   = inMode ? _cv("--tier-ram-bg", "#0a2a14") : _cv("--bg3", "#161616");
      node.bgcolor = inMode ? _cv("--bg2", "#0f0f0f") : _cv("--bg2", "#0f0f0f");
    });
    if (typeof canvas !== "undefined") canvas.setDirty(true, true);
  }
};
