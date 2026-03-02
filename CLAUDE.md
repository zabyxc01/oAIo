# oAIo — Claude Code Context

> Cold-start prompt. Read this before making any changes.
> Full ignition doc: `docs/oAIo-path-to-alpha-1.0.md`

---

## What This Is

oAIo is a local AI workstation orchestration platform. Not an AI tool — the layer that makes all AI tools aware of each other, enforces shared GPU/RAM constraints, and presents 7 Docker containers as one coherent system.

**The pitch:** OctoPrint for AI infrastructure.

---

## System — SYS-PANDORA-OAO
- CPU: AMD Ryzen 9 3900X | RAM: 62GB | GPU: RX 7900 XT 20GB (gfx1100, RDNA3)
- OS: Ubuntu 22.04.5 LTS | ROCm 6.2 | Python 3.10
- VRAM sysfs: `/sys/class/drm/card1/device/mem_info_vram_used|total|gpu_busy_percent`
- Always use `python3`. Prefer Docker over system pip installs.

---

## Project Location
```
/mnt/storage/oAIo/
  backend/
    ollmo/main.py       ← oLLMo API port 9000 — primary control plane
    oaudio/main.py      ← oAudio API port 8002 — voice pipeline
    core/
      vram.py           ← sysfs VRAM reader (no rocm-smi)
      resources.py      ← VRAM projection + system accounting
      enforcer.py       ← reactive OOM loop + register_manual_stop()
      paths.py          ← symlink management
      docker_control.py ← Docker SDK (stop timeout=3, non-blocking thread)
      extensions.py     ← extension loader
  config/
    services.json       ← 6 services: priority/limit_mode/auto_restore/vram_est_gb
    modes.json          ← 7 activation modes with budgets + allocations
    paths.json          ← 16 symlink entries (all green via /mnt/oaio/)
    routing.json        ← feature URL mapping
  frontend/src/
    index.html          ← 3-tab shell; OLLMO_API declared HERE ONCE
    app.js              ← tab switching, WS → cards, mode confirm flow
    panels/timeline.js  ← rolling 120s heatmap
    extensions-loader.js← queues extension node scripts for lazy load
    litegraph.js/.css   ← lazy-loaded only when CONFIG tab clicked
    style.css           ← all styles
  extensions/
    fleet/              ← multi-node orchestration (enabled)
    debugger/           ← live log streaming (enabled)
  docker-compose.yml    ← all 7 containers, all volumes via /mnt/oaio/*
  install.sh            ← symlinks + systemd setup
  docs/
    oAIo-path-to-alpha-1.0.md ← full ignition doc + roadmap
```

---

## Docker Stack
| Container | Port(s) | Notes |
|---|---|---|
| oaio | 9000, 8002 | Control plane + voice pipeline |
| ollama | 11434 | LLM inference (ROCm) — SATA models |
| open-webui | 3000 | Chat UI |
| kokoro-tts | 8000 | TTS synthesis |
| rvc | 7865, 8001 | Voice conversion proxy |
| f5-tts | 7860 | Voice cloning (Gradio API) |
| comfyui | 8188 | Image generation — Flux.1-dev |

**Run:** `cd /mnt/storage/oAIo && docker compose up -d`
**Rebuild:** `docker compose build oaio && docker compose up -d oaio`

---

## Critical Rules — Never Break
- **NEVER** delete native installs (ComfyUI at ~/ComfyUI/ stays)
- **Do NOT** modify `/etc/fstab`, PipeWire, ROCm, systemd without explicit user approval
- `sudo` always requires user approval
- `OLLMO_API` declared **ONCE** in `index.html` as `window.location.origin`
- sysfs only for VRAM — **no rocm-smi**
- All volumes go through `/mnt/oaio/*` — zero hardcoded personal paths in compose
- Enforcement loop pauses when no mode is active — safe for gaming

---

## Symlink Bus — /mnt/oaio/ (16 links, all green)
All service data routes through `/mnt/oaio/` symlinks. Swapping a symlink target instantly redirects any service's data path (NVMe ↔ RAM tier ↔ SATA) with zero downtime.

---

## Key Architecture Decisions
- **WS 1Hz push** — `GET /ws` streams vram/gpu/ram/services/accounting/kill_log/active_modes
- **Static files** — served with `Cache-Control: no-store` via `_NoCacheStatic` in main.py
- **LiteGraph lazy-load** — litegraph.js (~1MB) only loads when CONFIG tab clicked
- **Extension node scripts** — queued in `window._pendingExtNodes`, loaded with LiteGraph
- **Stop latency** — `threading.Thread(target=stop)` + `c.stop(timeout=3)` — instant response
- **Manual stop awareness** — `register_manual_stop()` prevents enforcer restart loop
- **auto_restore** field per service — toggle in UI or `PATCH /config/services/{name}`

---

## API Endpoints (port 9000)
```
WS   /ws                          1Hz push stream
GET  /system/status               full snapshot
GET  /vram                        GPU VRAM only
GET  /enforcement/status          kill order + kill log

POST /services/{name}/start|stop  start=direct, stop=background thread
GET  /services/{name}/status|logs

GET  /modes                       list all modes
POST /modes/{name}/activate       starts services, registers with enforcer
POST /modes/{name}/deactivate
GET  /modes/{name}/check          dry-run VRAM projection

GET  /config/services             service registry
POST /config/services             register new service
PATCH /config/services/{name}     update fields (auto_restore, priority, etc.)
GET/POST /config/paths            symlink management
POST /config/paths/{name}         repoint ("ram" or "default" shortcuts)
GET/POST /config/routing
GET  /config/storage/stats

GET  /extensions
POST /extensions/{name}/enable|disable
WS   /extensions/fleet/ws
WS   /extensions/debugger/ws/{container}
```

## oAudio (port 8002)
```
POST /speak     text → Kokoro → RVC → MP3
POST /convert   audio → RVC → MP3
POST /clone     ref_audio + text → F5-TTS → WAV (ref_text auto-transcribed if omitted)
GET  /voices
```

---

## Resource Enforcement
- Polls every 5s, **only when a mode is active**
- Kill order: highest priority number first (5=die first, 1=protected)
- `limit_mode: soft` = warn only | `hard` = auto-kill on OOM
- `auto_restore: true/false` — controls whether enforcer restarts after kill
- `register_manual_stop(ctr, svc_name, priority)` — suppresses crash detection for manual stops
- Recovery skips `reason="manual"` and `auto_restore=false`
- WARN at 85% | HARD kill at 95%
- Current: ollama/rvc/kokoro=soft | comfyui(3)/open-webui(4)/f5-tts(5)=hard

---

## UI — 3-Tab Fluid Grid
**LIVE** — mode select, kill log, services (start/stop/auto-restore toggle), RAM tier, timeline heatmap
**CONFIG** — LiteGraph placeholder (canvas in DOM hidden, node registration still works)
**ADVANCED** — storage paths, routing URLs, templates, API reference

Timeline view pills all default off — user selects which rows to show.

---

## Parked Work (do not implement unless asked)
- **CONFIG tab** — LiteGraph nodes, templates with boot order, RAM tier per-template, accounting card
- **RAM tier** — end-to-end verification; CONFIG/template integration
- **Install script** — with/without UI option (headless profile)
- **ADVANCED tab** — verify routing form + paths editor save correctly
- **WS reconnect** — clean recovery if connection drops
- **boot_with_system** — oaio boot sequence should respect the flag
- **f5-tts OOM** — system RAM pressure on startup, no enforcer coverage for host RAM OOM

---

## Git / GitHub
- Repo: https://github.com/zabyxc01/oAIo (public)
- Branch: main
- `.env` excluded from git (secrets) — copy `.env.example` to start
- `config/active_modes.json` excluded (runtime state)
- `templates/*.json` excluded (user runtime data)
