/**
 * Timeline — VRAM usage + aggregate rows (RAM/GPU/NVMe/SATA)
 * Left = oldest, right = newest (120-tick history)
 * All bars heat-colored (green→yellow→red) based on % of limit
 */

const HISTORY_LENGTH = 120;

const AGG_ROWS = {
  ram:    { label: 'RAM',    limit: 62,   color: null       },
  gpu:    { label: 'GPU%',   limit: 100,  color: null       },
  nvme_r: { label: 'NVMe R', limit: 3500, color: '#4a9eff'  },
  nvme_w: { label: 'NVMe W', limit: 3000, color: '#2266cc'  },
  sata_r: { label: 'SATA R', limit: 600,  color: '#f06292'  },
  sata_w: { label: 'SATA W', limit: 600,  color: '#9c27b0'  },
};
const AGG_ORDER = ['ram', 'gpu', 'nvme_r', 'nvme_w', 'sata_r', 'sata_w'];
const ROW_H = 14; // px per aggregate row

function heatColor(pct) {
  if (pct < 0.6)  return '#4caf50';
  if (pct < 0.85) return '#ffb300';
  return '#f44336';
}

let _vramTotal = 21;
const _history = new Array(HISTORY_LENGTH).fill(null);

function _pushTick(data) {
  if (data?.vram?.total_gb) _vramTotal = data.vram.total_gb;

  _history.shift();
  _history.push({
    vram:   (data?.vram?.used_gb  || 0) / _vramTotal,
    ram:    data?.ram?.used_gb                || 0,
    gpu:    (data?.gpu?.gpu_use_percent || 0) / 100,
    nvme_r: data?.storage?.nvme?.read_mbs     || 0,
    nvme_w: data?.storage?.nvme?.write_mbs    || 0,
    sata_r: data?.storage?.sata?.read_mbs     || 0,
    sata_w: data?.storage?.sata?.write_mbs    || 0,
  });
}

function drawTimeline(canvas, data, views) {
  const ctx = canvas.getContext('2d');
  const W   = canvas.width  = canvas.offsetWidth;
  const H   = canvas.height = canvas.offsetHeight;
  if (W === 0 || H === 0) return;

  if (data) _pushTick(data);

  ctx.clearRect(0, 0, W, H);

  const showVram  = !views || views.has('vram');
  const aggRows   = views ? AGG_ORDER.filter(k => views.has(k)) : AGG_ORDER;
  const totalRows = (showVram ? 1 : 0) + aggRows.length;
  const rowH      = totalRows > 0 ? Math.floor(H / totalRows) : H;
  const vramH     = showVram ? rowH : 0;
  const colW      = W / HISTORY_LENGTH;

  // ── VRAM area ───────────────────────────────────────────────
  if (vramH > 0) {
    _history.forEach((snap, i) => {
      const x  = Math.floor(i * colW);
      const cw = Math.max(1, Math.ceil(colW) - 1);
      ctx.fillStyle = '#0d0d0d';
      ctx.fillRect(x, 0, cw, vramH);
      if (!snap || snap.vram < 0.005) return;
      const pct = Math.min(snap.vram, 1);
      const h   = Math.max(1, Math.ceil(pct * vramH));
      ctx.fillStyle = heatColor(pct) + 'cc';
      ctx.fillRect(x, vramH - h, cw, h);
    });

    // 85% threshold line
    const thY = Math.round(vramH * 0.15);
    ctx.save();
    ctx.strokeStyle = '#ff000055';
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, thY); ctx.lineTo(W, thY);
    ctx.stroke();
    ctx.restore();
  }

  // ── Aggregate rows ──────────────────────────────────────────
  aggRows.forEach((key, idx) => {
    const cfg  = AGG_ROWS[key];
    const rowY = vramH + idx * rowH;

    ctx.fillStyle = '#0a0a0a';
    ctx.fillRect(0, rowY, W, rowH - 1);

    _history.forEach((snap, i) => {
      if (!snap) return;
      const val = snap[key] || 0;
      const pct = Math.min(val / cfg.limit, 1);
      if (pct < 0.005) return;
      const x  = Math.floor(i * colW);
      const cw = Math.max(1, Math.ceil(colW) - 1);
      const h  = Math.max(1, Math.ceil(pct * (rowH - 3)));
      ctx.fillStyle = (cfg.color || heatColor(pct)) + 'cc';
      ctx.fillRect(x, rowY + rowH - 2 - h, cw, h);
    });
  });

  // ── HTML row labels overlay ─────────────────────────────────
  _updateRowLabels(vramH, aggRows, rowH);
}

function _updateRowLabels(vramH, aggRows, rowH) {
  const el = document.getElementById('timeline-row-labels');
  if (!el) return;

  const items = [];
  if (vramH > 0) {
    items.push({ label: 'VRAM', top: vramH / 2 - 5 });
  }
  aggRows.forEach((key, idx) => {
    const rowY = vramH + idx * rowH;
    items.push({ label: AGG_ROWS[key].label, top: rowY + rowH / 2 - 5 });
  });

  el.innerHTML = items.map(({ label, top }) =>
    `<span class="tl-row-label" style="top:${top}px">${label}</span>`
  ).join('');
}

function _renderLegend() {
  const el = document.getElementById('timeline-legend');
  if (!el) return;

  const items = [
    { color: '#4caf50', label: '<60%' },
    { color: '#ffb300', label: '60–85%' },
    { color: '#f44336', label: '>85%' },
    { color: null },
    { color: '#4a9eff', label: 'NVMe R' },
    { color: '#2266cc', label: 'NVMe W' },
    { color: '#f06292', label: 'SATA R' },
    { color: '#9c27b0', label: 'SATA W' },
  ];

  el.innerHTML = items.map(({ color, label }) =>
    !color
      ? `<span class="tl-legend-sep"></span>`
      : `<span class="tl-legend-item"><span class="tl-legend-dot" style="background:${color}"></span>${label}</span>`
  ).join('');
}

// Render legend immediately on load — doesn't depend on canvas being visible
_renderLegend();

window.Timeline = { drawTimeline };
