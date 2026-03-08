/**
 * M³ (MultiModelMode) — LiteGraph node types
 * Registered by extensions-loader.js when the m3 extension is enabled.
 *
 * Node types:
 *   oAIo/m3-model     — Ollama model in a pipeline
 *   oAIo/m3-input     — Pipeline entry point
 *   oAIo/m3-output    — Pipeline exit point
 *   oAIo/m3-transform — Text transform between steps
 *   oAIo/m3-router    — Conditional routing
 *   oAIo/m3-training  — Training job node
 */

(function () {

  // ── Helpers ──────────────────────────────────────────────────────────────────

  /** Read CSS custom property with fallback */
  function _cv(name, fb) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fb;
  }

  /** Build a 12%-tint bgcolor from a hex color like #a855f7 */
  function _tint(hex) {
    var r = parseInt(hex.slice(1, 3), 16);
    var g = parseInt(hex.slice(3, 5), 16);
    var b = parseInt(hex.slice(5, 7), 16);
    return "rgba(" + r + "," + g + "," + b + ",0.12)";
  }

  /** Truncate a string to maxLen, appending ellipsis if needed */
  function _trunc(s, maxLen) {
    if (!s) return "";
    return s.length > maxLen ? s.slice(0, maxLen) + "\u2026" : s;
  }

  // Palette
  var COLOR_PURPLE   = "#a855f7";
  var COLOR_GREEN    = "#22c55e";
  var COLOR_CORAL    = "#f97316";
  var COLOR_AMBER    = "#f59e0b";
  var COLOR_CYAN     = "#06b6d4";
  var COLOR_DEEP_PUR = "#7c3aed";

  var BG_PURPLE   = _tint(COLOR_PURPLE);
  var BG_GREEN    = _tint(COLOR_GREEN);
  var BG_CORAL    = _tint(COLOR_CORAL);
  var BG_AMBER    = _tint(COLOR_AMBER);
  var BG_CYAN     = _tint(COLOR_CYAN);
  var BG_DEEP_PUR = _tint(COLOR_DEEP_PUR);

  var TITLE_COLOR = _cv("--bg3", "#161616");
  var TEXT_DIM    = _cv("--text-dim", "#555");
  var TEXT_LIGHT  = _cv("--text", "#e0e0e0");


  // ═══════════════════════════════════════════════════════════════════════════
  // 1. M3 Model Node — represents an Ollama model in a pipeline
  // ═══════════════════════════════════════════════════════════════════════════

  function M3ModelNode() {
    this.addInput("input", "string");
    this.addOutput("output", "string");

    this.title    = "Model";
    this.color    = TITLE_COLOR;
    this.bgcolor  = BG_PURPLE;
    this.size     = [200, 120];
    this.resizable = true;

    this.addProperty("model", "");
    this.addProperty("prompt_template", "{{input}}");
    this.addProperty("temperature", 0.7);
    this.addProperty("max_tokens", 2048);

    this._status      = "idle";    // idle | running | error
    this._modelList   = [];        // cached model names for combo
    this._lastOutput  = "";

    // Widgets
    this._comboWidget = this.addWidget("combo", "model", "", this._onModelChange.bind(this), { values: [] });
    this._tempWidget  = this.addWidget("number", "temperature", 0.7, this._onTempChange.bind(this), { min: 0, max: 2, step: 0.1, precision: 2 });

    // Populate model list on creation
    this._fetchModels();
  }

  M3ModelNode.prototype = Object.create(LGraphNode.prototype);
  M3ModelNode.title = "Model";

  M3ModelNode.prototype._onModelChange = function (v) {
    this.properties.model = v;
    this.title = v || "Model";
    this.setDirtyCanvas(true);
  };

  M3ModelNode.prototype._onTempChange = function (v) {
    this.properties.temperature = v;
  };

  M3ModelNode.prototype._fetchModels = async function () {
    try {
      var r = await fetch(OLLMO_API + "/services/ollama/models");
      if (!r.ok) return;
      var data = await r.json();
      var names = (data.models || data || []).map(function (m) {
        return typeof m === "string" ? m : m.name || m.model || "";
      }).filter(Boolean);
      this._modelList = names;
      if (this._comboWidget) {
        this._comboWidget.options.values = names;
      }
      this.setDirtyCanvas(true);
    } catch (e) {
      // silent — Ollama may not be running
    }
  };

  M3ModelNode.prototype.onDblClick = function () {
    // Refresh model list on double-click
    this._fetchModels();
  };

  M3ModelNode.prototype.onDrawForeground = function (ctx) {
    // Status indicator dot
    var dotColor = this._status === "running" ? _cv("--green", "#00e676")
                 : this._status === "error"   ? _cv("--red", "#ff1744")
                 : COLOR_PURPLE;
    ctx.fillStyle = dotColor;
    ctx.beginPath();
    ctx.arc(this.size[0] - 14, 14, 4, 0, Math.PI * 2);
    ctx.fill();

    // Model name badge at bottom
    ctx.fillStyle = TEXT_DIM;
    ctx.font = "10px monospace";
    var modelLabel = this.properties.model || "(no model)";
    ctx.fillText(_trunc(modelLabel, 28), 8, this.size[1] - 24);
    ctx.fillText("temp: " + this.properties.temperature.toFixed(2) + "  max: " + this.properties.max_tokens, 8, this.size[1] - 10);
  };

  M3ModelNode.prototype.onExecute = function () {
    var input = this.getInputData(0);
    if (input === undefined || input === null) return;

    var template = this.properties.prompt_template || "{{input}}";
    var prompt = template.replace(/\{\{input\}\}/g, input);

    // Signal that execution is pending — actual inference runs via backend
    this._status = "running";
    this.setDirtyCanvas(true);

    var self = this;
    fetch(OLLMO_API + "/extensions/m3/infer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model:       self.properties.model,
        prompt:      prompt,
        temperature: self.properties.temperature,
        max_tokens:  self.properties.max_tokens,
      }),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        self._lastOutput = d.response || d.text || "";
        self._status = "idle";
        self.setOutputData(0, self._lastOutput);
        self.setDirtyCanvas(true);
      })
      .catch(function () {
        self._status = "error";
        self.setDirtyCanvas(true);
      });
  };

  M3ModelNode.prototype.configure = function (data) {
    LGraphNode.prototype.configure.call(this, data);
    if (data.properties) {
      if (data.properties.model) this.title = data.properties.model;
      if (this._comboWidget) this._comboWidget.value = data.properties.model || "";
      if (this._tempWidget)  this._tempWidget.value  = data.properties.temperature != null ? data.properties.temperature : 0.7;
    }
    this._fetchModels();
  };


  // ═══════════════════════════════════════════════════════════════════════════
  // 2. M3 Input Node — pipeline entry point
  // ═══════════════════════════════════════════════════════════════════════════

  function M3InputNode() {
    this.addOutput("text", "string");

    this.title    = "Pipeline Input";
    this.color    = TITLE_COLOR;
    this.bgcolor  = BG_GREEN;
    this.size     = [160, 60];
    this.resizable = true;

    this.addProperty("default_text", "");

    this._textWidget = this.addWidget("text", "text", "", this._onTextChange.bind(this));
  }

  M3InputNode.prototype = Object.create(LGraphNode.prototype);
  M3InputNode.title = "Pipeline Input";

  M3InputNode.prototype._onTextChange = function (v) {
    this.properties.default_text = v;
  };

  M3InputNode.prototype.onExecute = function () {
    this.setOutputData(0, this.properties.default_text || "");
  };

  M3InputNode.prototype.configure = function (data) {
    LGraphNode.prototype.configure.call(this, data);
    if (data.properties && this._textWidget) {
      this._textWidget.value = data.properties.default_text || "";
    }
  };


  // ═══════════════════════════════════════════════════════════════════════════
  // 3. M3 Output Node — pipeline exit point
  // ═══════════════════════════════════════════════════════════════════════════

  function M3OutputNode() {
    this.addInput("text", "string");

    this.title    = "Pipeline Output";
    this.color    = TITLE_COLOR;
    this.bgcolor  = BG_CORAL;
    this.size     = [160, 80];
    this.resizable = true;

    this.addProperty("last_result", "");
  }

  M3OutputNode.prototype = Object.create(LGraphNode.prototype);
  M3OutputNode.title = "Pipeline Output";

  M3OutputNode.prototype.onExecute = function () {
    var input = this.getInputData(0);
    if (input !== undefined && input !== null) {
      this.properties.last_result = String(input);
      this.setDirtyCanvas(true);
    }
  };

  M3OutputNode.prototype.onDrawForeground = function (ctx) {
    var result = this.properties.last_result || "";
    if (!result) return;

    ctx.fillStyle = TEXT_DIM;
    ctx.font = "9px monospace";

    // Word-wrap the first 100 chars across a few lines
    var display = _trunc(result, 100);
    var lines = [];
    var lineLen = 24;
    for (var i = 0; i < display.length; i += lineLen) {
      lines.push(display.slice(i, i + lineLen));
    }

    var yStart = 6;
    for (var j = 0; j < Math.min(lines.length, 4); j++) {
      ctx.fillText(lines[j], 8, yStart + j * 12);
    }
    if (lines.length > 4) {
      ctx.fillText("\u2026", 8, yStart + 4 * 12);
    }
  };


  // ═══════════════════════════════════════════════════════════════════════════
  // 4. M3 Transform Node — text transform between steps
  // ═══════════════════════════════════════════════════════════════════════════

  function M3TransformNode() {
    this.addInput("input", "string");
    this.addOutput("output", "string");

    this.title    = "Transform";
    this.color    = TITLE_COLOR;
    this.bgcolor  = BG_AMBER;
    this.size     = [180, 100];
    this.resizable = true;

    this.addProperty("mode", "template");
    this.addProperty("template", "{{input}}");
    this.addProperty("append_text", "");

    this._modeWidget = this.addWidget("combo", "mode", "template", this._onModeChange.bind(this), {
      values: ["template", "extract_json", "summarize_prefix", "append"],
    });
  }

  M3TransformNode.prototype = Object.create(LGraphNode.prototype);
  M3TransformNode.title = "Transform";

  M3TransformNode.prototype._onModeChange = function (v) {
    this.properties.mode = v;
    this.setDirtyCanvas(true);
  };

  M3TransformNode.prototype.onExecute = function () {
    var input = this.getInputData(0);
    if (input === undefined || input === null) return;

    var output = input;
    var mode = this.properties.mode;

    if (mode === "template") {
      var tmpl = this.properties.template || "{{input}}";
      output = tmpl.replace(/\{\{input\}\}/g, input);
    } else if (mode === "extract_json") {
      // Attempt to extract first JSON block from input
      var match = input.match(/\{[\s\S]*\}/);
      output = match ? match[0] : input;
    } else if (mode === "summarize_prefix") {
      // Prepend a summarization instruction
      output = "Summarize the following:\n\n" + input;
    } else if (mode === "append") {
      output = input + (this.properties.append_text || "");
    }

    this.setOutputData(0, output);
  };

  M3TransformNode.prototype.onDrawForeground = function (ctx) {
    ctx.fillStyle = TEXT_DIM;
    ctx.font = "10px monospace";
    ctx.fillText("mode: " + this.properties.mode, 8, this.size[1] - 10);
  };

  M3TransformNode.prototype.configure = function (data) {
    LGraphNode.prototype.configure.call(this, data);
    if (data.properties && this._modeWidget) {
      this._modeWidget.value = data.properties.mode || "template";
    }
  };


  // ═══════════════════════════════════════════════════════════════════════════
  // 5. M3 Router Node — conditional routing
  // ═══════════════════════════════════════════════════════════════════════════

  function M3RouterNode() {
    this.addInput("input", "string");
    this.addOutput("match", "string");
    this.addOutput("no_match", "string");

    this.title    = "Router";
    this.color    = TITLE_COLOR;
    this.bgcolor  = BG_CYAN;
    this.size     = [180, 100];
    this.resizable = true;

    this.addProperty("condition", "contains");
    this.addProperty("pattern", "");

    this._condWidget = this.addWidget("combo", "condition", "contains", this._onCondChange.bind(this), {
      values: ["contains", "starts_with", "regex"],
    });
  }

  M3RouterNode.prototype = Object.create(LGraphNode.prototype);
  M3RouterNode.title = "Router";

  M3RouterNode.prototype._onCondChange = function (v) {
    this.properties.condition = v;
    this.setDirtyCanvas(true);
  };

  M3RouterNode.prototype.onExecute = function () {
    var input = this.getInputData(0);
    if (input === undefined || input === null) return;

    var matched = false;
    var pattern = this.properties.pattern || "";
    var cond = this.properties.condition;

    if (cond === "contains") {
      matched = input.indexOf(pattern) !== -1;
    } else if (cond === "starts_with") {
      matched = input.indexOf(pattern) === 0;
    } else if (cond === "regex") {
      try {
        matched = new RegExp(pattern).test(input);
      } catch (e) {
        matched = false;
      }
    }

    if (matched) {
      this.setOutputData(0, input);   // match
      this.setOutputData(1, null);    // no_match
    } else {
      this.setOutputData(0, null);    // match
      this.setOutputData(1, input);   // no_match
    }
  };

  M3RouterNode.prototype.onDrawForeground = function (ctx) {
    ctx.fillStyle = TEXT_DIM;
    ctx.font = "10px monospace";
    ctx.fillText(this.properties.condition + ": " + _trunc(this.properties.pattern, 18), 8, this.size[1] - 10);
  };

  M3RouterNode.prototype.configure = function (data) {
    LGraphNode.prototype.configure.call(this, data);
    if (data.properties && this._condWidget) {
      this._condWidget.value = data.properties.condition || "contains";
    }
  };


  // ═══════════════════════════════════════════════════════════════════════════
  // 6. M3 Training Node — training job on the canvas
  // ═══════════════════════════════════════════════════════════════════════════

  function M3TrainingNode() {
    this.addOutput("adapter", "string");

    this.title    = "Training Job";
    this.color    = TITLE_COLOR;
    this.bgcolor  = BG_DEEP_PUR;
    this.size     = [200, 130];
    this.resizable = true;

    this.addProperty("job_id", "");
    this.addProperty("base_model", "");
    this.addProperty("method", "qlora");
    this.addProperty("status", "idle");

    this._progress  = 0;          // 0-100
    this._adapter   = "";         // path to trained adapter
    this._polling   = null;

    this._methodWidget = this.addWidget("combo", "method", "qlora", this._onMethodChange.bind(this), {
      values: ["qlora", "lora", "full"],
    });
  }

  M3TrainingNode.prototype = Object.create(LGraphNode.prototype);
  M3TrainingNode.title = "Training Job";

  M3TrainingNode.prototype._onMethodChange = function (v) {
    this.properties.method = v;
  };

  M3TrainingNode.prototype._startPolling = function () {
    if (this._polling) return;
    var self = this;
    this._polling = setInterval(function () { self._refresh(); }, 5000);
    this._refresh();
  };

  M3TrainingNode.prototype._stopPolling = function () {
    if (this._polling) {
      clearInterval(this._polling);
      this._polling = null;
    }
  };

  M3TrainingNode.prototype._refresh = async function () {
    if (!this.properties.job_id) return;
    try {
      var r = await fetch(OLLMO_API + "/extensions/m3/training/jobs/" + encodeURIComponent(this.properties.job_id));
      if (!r.ok) return;
      var d = await r.json();
      this.properties.status     = d.status || "unknown";
      this.properties.base_model = d.base_model || this.properties.base_model;
      this.properties.method     = d.method || this.properties.method;
      this._progress             = d.progress || 0;
      this._adapter              = d.adapter_path || "";

      if (this._adapter) {
        this.setOutputData(0, this._adapter);
      }

      // Stop polling if job is terminal
      if (d.status === "completed" || d.status === "failed" || d.status === "cancelled") {
        this._stopPolling();
      }

      if (this._methodWidget) {
        this._methodWidget.value = this.properties.method;
      }
      this.setDirtyCanvas(true);
    } catch (e) {
      // silent
    }
  };

  M3TrainingNode.prototype.onRemoved = function () {
    this._stopPolling();
  };

  M3TrainingNode.prototype.onDrawForeground = function (ctx) {
    var status = this.properties.status || "idle";

    // Status color
    var statusColor = status === "running"   ? _cv("--green", "#00e676")
                    : status === "completed" ? COLOR_PURPLE
                    : status === "failed"    ? _cv("--red", "#ff1744")
                    : TEXT_DIM;

    // Method badge (top-right)
    ctx.save();
    ctx.fillStyle = COLOR_DEEP_PUR;
    ctx.globalAlpha = 0.35;
    var badgeText = (this.properties.method || "qlora").toUpperCase();
    var metrics = ctx.measureText(badgeText);
    var badgeW = metrics.width + 12;
    ctx.fillRect(this.size[0] - badgeW - 6, 4, badgeW, 16);
    ctx.globalAlpha = 1.0;
    ctx.fillStyle = TEXT_LIGHT;
    ctx.font = "bold 9px monospace";
    ctx.fillText(badgeText, this.size[0] - badgeW - 1, 15);
    ctx.restore();

    // Info lines
    ctx.fillStyle = TEXT_DIM;
    ctx.font = "10px monospace";
    ctx.fillText("model: " + _trunc(this.properties.base_model || "(none)", 22), 8, this.size[1] - 52);
    ctx.fillText("job:   " + _trunc(this.properties.job_id || "(none)", 22), 8, this.size[1] - 38);

    // Status text
    ctx.fillStyle = statusColor;
    ctx.fillText("status: " + status, 8, this.size[1] - 24);

    // Progress bar (only when running or completed or has progress)
    if (status === "running" || status === "completed" || this._progress > 0) {
      var barX = 8;
      var barY = this.size[1] - 14;
      var barW = this.size[0] - 16;
      var barH = 6;

      // Track
      ctx.fillStyle = "rgba(255,255,255,0.06)";
      ctx.fillRect(barX, barY, barW, barH);

      // Fill
      var pct = Math.max(0, Math.min(100, this._progress)) / 100;
      ctx.save();
      ctx.fillStyle = statusColor;
      ctx.globalAlpha = 0.7;
      ctx.fillRect(barX, barY, barW * pct, barH);
      ctx.restore();

      // Percentage label
      if (this._progress > 0) {
        ctx.fillStyle = TEXT_LIGHT;
        ctx.font = "8px monospace";
        var pctLabel = Math.round(this._progress) + "%";
        ctx.fillText(pctLabel, barX + barW - ctx.measureText(pctLabel).width - 2, barY + 5);
      }
    }
  };

  M3TrainingNode.prototype.configure = function (data) {
    LGraphNode.prototype.configure.call(this, data);
    if (data.properties) {
      if (this._methodWidget) this._methodWidget.value = data.properties.method || "qlora";
      // Resume polling if job is still active
      if (data.properties.job_id && data.properties.status === "running") {
        this._startPolling();
      }
    }
  };

  M3TrainingNode.prototype.onPropertyChanged = function (name, value) {
    if (name === "job_id" && value) {
      this._startPolling();
    }
  };


  // ═══════════════════════════════════════════════════════════════════════════
  // Registration
  // ═══════════════════════════════════════════════════════════════════════════

  LiteGraph.registerNodeType("oAIo/m3-model",     M3ModelNode);
  LiteGraph.registerNodeType("oAIo/m3-input",     M3InputNode);
  LiteGraph.registerNodeType("oAIo/m3-output",    M3OutputNode);
  LiteGraph.registerNodeType("oAIo/m3-transform", M3TransformNode);
  LiteGraph.registerNodeType("oAIo/m3-router",    M3RouterNode);
  LiteGraph.registerNodeType("oAIo/m3-training",  M3TrainingNode);

  console.log("[oAIo] M\u00b3 nodes registered (6 types)");

})();
