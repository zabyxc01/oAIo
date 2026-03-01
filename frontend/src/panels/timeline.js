/**
 * Timeline heatmap — VRAM / RAM / GPU over time
 * Left = highest priority (last to close)
 * Right = hot swappable (first to park)
 * Color: green → yellow → red based on % of limit
 * views: Set of row keys to render ('vram','ram','gpu')
 */

const HISTORY_LENGTH = 120;
const ROWS       = ["vram", "ram", "gpu", "nvme_r", "nvme_w", "sata_r", "sata_w"];
const ROW_LABELS = {
  vram:   "VRAM",
  ram:    "RAM",
  gpu:    "GPU%",
  nvme_r: "NVMe R",
  nvme_w: "NVMe W",
  sata_r: "SATA R",
  sata_w: "SATA W",
};
const LIMITS = {
  vram:   20,
  ram:    62,
  gpu:    100,
  nvme_r: 3500,   // MB/s
  nvme_w: 3000,
  sata_r: 600,
  sata_w: 600,
};

const history = {
  vram:   new Array(HISTORY_LENGTH).fill(0),
  ram:    new Array(HISTORY_LENGTH).fill(0),
  gpu:    new Array(HISTORY_LENGTH).fill(0),
  nvme_r: new Array(HISTORY_LENGTH).fill(0),
  nvme_w: new Array(HISTORY_LENGTH).fill(0),
  sata_r: new Array(HISTORY_LENGTH).fill(0),
  sata_w: new Array(HISTORY_LENGTH).fill(0),
};

// Custom color overrides (hex) — null = auto heatmap
const rowColors = {
  vram:   null,
  ram:    null,
  gpu:    null,
  nvme_r: "#4a9eff",
  nvme_w: "#2266cc",
  sata_r: "#ffb300",
  sata_w: "#cc8800",
};

function heatColor(pct, customHex) {
  if (customHex) return customHex;
  if (pct < 0.6)  return "#4caf50";
  if (pct < 0.85) return "#ffb300";
  return "#f44336";
}

function pushHistory(key, value) {
  history[key].shift();
  history[key].push(value);
}

function drawTimeline(canvas, data, views) {
  const ctx = canvas.getContext("2d");
  const W   = canvas.width  = canvas.offsetWidth;
  const H   = canvas.height = canvas.offsetHeight;

  ctx.clearRect(0, 0, W, H);

  // push new data first
  if (data) {
    pushHistory("vram",   data.vram?.used_gb           || 0);
    pushHistory("ram",    data.ram?.used_gb            || 0);
    pushHistory("gpu",    data.gpu?.gpu_use_percent    || 0);
    pushHistory("nvme_r", data.storage?.nvme?.read_mbs  || 0);
    pushHistory("nvme_w", data.storage?.nvme?.write_mbs || 0);
    pushHistory("sata_r", data.storage?.sata?.read_mbs  || 0);
    pushHistory("sata_w", data.storage?.sata?.write_mbs || 0);
  }

  const visibleRows = views ? ROWS.filter(r => views.has(r)) : ROWS;
  if (visibleRows.length === 0) return;

  const rowH = H / visibleRows.length;
  const colW = W / HISTORY_LENGTH;

  visibleRows.forEach((key, rowIdx) => {
    const y     = rowIdx * rowH;
    const limit = LIMITS[key];

    ctx.fillStyle = "#111";
    ctx.fillRect(0, y, W, rowH - 1);

    ctx.fillStyle = "#444";
    ctx.font = "9px monospace";
    ctx.fillText(ROW_LABELS[key], 4, y + rowH - 4);

    history[key].forEach((val, i) => {
      const pct   = Math.min(val / limit, 1);
      const color = heatColor(pct, rowColors[key]);
      const bH    = pct * (rowH - 2);
      ctx.fillStyle = color + "cc";
      ctx.fillRect(i * colW, y + (rowH - 2 - bH), colW - 1, bH);
    });

    const limitY = y + rowH - 2 - (rowH - 2) * 0.85;
    ctx.strokeStyle = "#ff000044";
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(0, limitY);
    ctx.lineTo(W, limitY);
    ctx.stroke();
    ctx.setLineDash([]);
  });
}

window.Timeline = { drawTimeline, rowColors, pushHistory };
