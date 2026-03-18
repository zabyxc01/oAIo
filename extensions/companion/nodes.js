/**
 * Companion Client — LiteGraph node for the CONFIG tab.
 * Shows connected companion clients (desktop, phone) as nodes in the graph.
 */
(function () {
  function CompanionClientNode() {
    this.addOutput("audio-out", "audio");
    this.addOutput("text-out", "text");
    this.addInput("text-in", "text");
    this.addInput("audio-in", "audio");

    this.properties = {
      client_type: "desktop",
      platform: "unknown",
      status: "disconnected",
    };

    this.size = [220, 100];
    this.color = "#3a1a5e";
    this.bgcolor = "#1a0a2e";
  }

  CompanionClientNode.title = "Companion Client";
  CompanionClientNode.desc = "Connected companion renderer (desktop/phone)";

  CompanionClientNode.prototype.onDrawForeground = function (ctx) {
    var status = this.properties.status || "disconnected";
    var platform = this.properties.platform || "unknown";
    var clientType = this.properties.client_type || "desktop";

    // Status indicator
    ctx.fillStyle = status === "connected" ? "#4ade80" : "#ef4444";
    ctx.beginPath();
    ctx.arc(this.size[0] - 20, 16, 6, 0, Math.PI * 2);
    ctx.fill();

    // Labels
    ctx.fillStyle = "#ccc";
    ctx.font = "11px monospace";
    ctx.fillText(clientType + " / " + platform, 10, 50);
    ctx.fillText(status, 10, 66);
  };

  CompanionClientNode.prototype.getExtraMenuOptions = function () {
    return [
      {
        content: "Refresh Status",
        callback: function () {
          fetch(OLLMO_API + "/extensions/companion/clients")
            .then(function (r) { return r.json(); })
            .then(function (clients) {
              console.log("[companion] clients:", clients);
            });
        },
      },
    ];
  };

  LiteGraph.registerNodeType("oAIo/companion-client", CompanionClientNode);
})();
