# oAIo — Claude Code Context

> Cold-start prompt. Read this before making any changes.

## MANDATORY: Path to Alpha 1.0 Rules

**Before touching ANY code in this project, read these files:**

1. `docs/Path to Alpha 1.0/Origin Ignition Prompt.md` — the master plan. This is the source of truth for all Alpha work.
2. `docs/Path to Alpha 1.0/_RULES.md` — the rules for documenting changes.

**The Origin Ignition Prompt is NEVER edited.** When you implement a phase or sub-phase:

1. **Create a new document** in `docs/Path to Alpha 1.0/` named `Phase-{N}{Letter}-{Short-Name}.md`
2. **Cite the original section** — quote the relevant plan text from the Origin
3. **Document what actually changed** — files modified, lines added/removed, commits
4. **Explain WHY** if anything deviated from the plan. No workarounds without explanation and user approval.
5. **No workarounds.** If the plan says X and you can't do X, stop and explain. Do not silently do Y instead.

**This is not optional. Skipping the change document is a rule violation.**

---

---

## What This Is

oAIo is a local AI workstation orchestration platform. Not an AI tool — the layer that makes all AI tools aware of each other, enforces shared GPU/RAM constraints, and presents 10 Docker containers as one coherent system.

**The pitch:** OctoPrint for AI infrastructure.

---

## System — SYS-PANDORA-OAO
- CPU: AMD Ryzen 9 3900X | RAM: 62GB | GPU: RX 7900 XT 20GB (gfx1100, RDNA3)
- OS: Ubuntu 24.04 LTS | ROCm (Docker only) | Python 3.10+
- VRAM sysfs: `/sys/class/drm/card1/device/mem_info_vram_used|total|gpu_busy_percent`
- Always use `python3`. Prefer Docker over system pip installs.

---

## Project Location
```
/mnt/windows-sata/oAIo/
  backend/
    ollmo/main.py       <- oLLMo API port 9000 — primary control plane
    oaudio/main.py      <- oAudio API port 8002 — voice pipeline
    core/
      vram.py           <- sysfs VRAM reader (no rocm-smi)
      resources.py      <- VRAM projection + system accounting
      enforcer.py       <- reactive OOM loop + register_manual_stop()
      paths.py          <- symlink management
      docker_control.py <- Docker SDK (stop timeout=3, non-blocking thread)
      extensions.py     <- extension loader
  config/
    services.json       <- 7 services: priority/limit_mode/auto_restore/vram_est_gb
    modes.json          <- 3 activation modes (oLLMo, oAudio, comfyui-flex) with budgets + allocations
    paths.json          <- 16 symlink entries (all green via /mnt/oaio/)
    routing.json        <- feature URL mapping
  frontend/src/
    index.html          <- 6-tab shell (LIVE, CONFIG, ADVANCED, API, SETTINGS, HELP); OLLMO_API declared HERE ONCE
    app.js              <- tab switching, WS -> cards, mode confirm flow, API tab, settings system
    extensions-loader.js<- queues extension node scripts for lazy load
    litegraph.js/.css   <- lazy-loaded only when CONFIG tab clicked
    style.css           <- all styles
  extensions/
    fleet/              <- multi-node orchestration (enabled)
    debugger/           <- live log streaming (enabled)
  docker/styletts2/     <- StyleTTS2 Dockerfile + app.py
  docker-compose.yml    <- all 8 containers, all volumes via /mnt/oaio/*
  install.sh            <- interactive installer (styletts2, model pulling, systemd)
  update.sh             <- backup + pull + rebuild + rolling restart + heal
  repair.sh             <- diagnose + fix symlinks + docker cleanup + HF dedup
  uninstall.sh          <- interactive 7-step uninstaller (never deletes model data)
  docs/
    SPECIFICATIONS.md     <- full technical spec (architecture, API, colors, security)
    oAIo-path-to-alpha-1.0.md <- full ignition doc + roadmap
```

---

## Docker Stack
| Container | Port(s) | Notes |
|---|---|---|
| oaio | 9000, 8002 | Control plane + voice pipeline |
| ollama | 11434 | LLM inference (ROCm) — SATA models |
| open-webui | 3000 | Chat UI — RAG via Ollama nomic-embed-text |
| kokoro-tts | 8000 | TTS synthesis |
| rvc | 7865, 8001 | Voice conversion proxy |
| f5-tts | 7860 | Voice cloning (Gradio API) |
| styletts2 | 7870 | StyleTTS2 voice prototyping (Gradio UI) |
| comfyui | 8188 | Image generation — Flux.1-dev |

**Run:** `cd /mnt/windows-sata/oAIo && docker compose up -d`
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
All service data routes through `/mnt/oaio/` symlinks. Swapping a symlink target instantly redirects any service's data path (NVMe <-> RAM tier <-> SATA) with zero downtime.

---

## Key Architecture Decisions
- **WS 1Hz push** — `GET /ws` streams vram/gpu/ram/services/accounting/kill_log/active_modes
- **Static files** — served with `Cache-Control: no-store` via `_NoCacheStatic` in main.py
- **LiteGraph lazy-load** — litegraph.js (~1MB) only loads when CONFIG tab clicked
- **Extension node scripts** — queued in `window._pendingExtNodes`, loaded with LiteGraph
- **Stop latency** — `threading.Thread(target=stop)` + `c.stop(timeout=3)` — instant response
- **Manual stop awareness** — `register_manual_stop()` prevents enforcer restart loop
- **auto_restore** field per service — toggle in UI or `PATCH /config/services/{name}`
- **Request logging middleware** — ASGI middleware logs all API requests to rolling deque(500)
- **OpenAPI tags** — all routes tagged for auto-generated endpoint grouping in API tab
- **Config lock** — `asyncio.Lock()` protects 8 read-modify-write endpoints
- **Docker reconnect** — `_get_client()` with `ping()` auto-reconnects stale clients
- **Atomic writes** — all JSON config writes use temp + rename
- **HTML escaping** — `_esc()` on all user-data in innerHTML (~40 call sites)
- **Mode displacement** — activating a new mode stops displaced services first
- **Live thresholds** — enforcer reads `resources.HARD_THRESHOLD` via module ref (not frozen import)
- **Group node colors** — nodes colored by service group (12% tint of group accent)

---

## API Endpoints (port 9000)
```
WS   /ws                          1Hz push stream
GET  /system/status               full snapshot
GET  /vram                        GPU VRAM only
GET  /enforcement/status          kill order + kill log
POST /enforcement/enable|disable  master enforcer toggle
POST /emergency/kill              kill all containers immediately

POST /services/{name}/start|stop  start=direct, stop=background thread
GET  /services/{name}/status|logs
GET  /services/ollama/models      list Ollama models
POST /services/ollama/models/{name}/load   load model
POST /services/ollama/models/pull          pull new model
DELETE /services/ollama/models/{name}      delete model
GET  /services/rvc/models
POST /services/rvc/models/{name}/activate
GET  /services/comfyui/workflows

GET  /modes                       list all modes
POST /modes                       create mode
POST /modes/{name}/activate       starts services, registers with enforcer
POST /modes/{name}/deactivate
GET  /modes/{name}/check          dry-run VRAM projection
POST /modes/{name}/reset          restore defaults

GET  /config/services             service registry
POST /config/services             register new service
PATCH /config/services/{name}     update fields (auto_restore, priority, etc.)
GET/POST /config/paths            symlink management
POST /config/paths/{name}         repoint ("ram" or "default" shortcuts)
DELETE /config/paths/{name}       remove symlink
GET/POST /config/routing
GET  /config/storage/stats
GET/POST /config/nodes            LiteGraph node positions

GET  /templates                   list templates
POST /templates/{name}/load       load template
GET  /benchmark/history           rolling 5-min VRAM/GPU history (300 samples)

GET  /api/monitor/stream          last N logged requests
GET  /api/monitor/stats           aggregated request stats
WS   /api/monitor/ws              live request push

GET  /extensions
POST /extensions/{name}/enable|disable
WS   /extensions/fleet/ws
WS   /extensions/debugger/ws/{container}
```

## oAudio (port 8002)
```
POST /speak     text -> Kokoro -> RVC -> MP3
POST /convert   audio -> RVC -> MP3
POST /clone     ref_audio + text -> F5-TTS -> WAV (ref_text auto-transcribed if omitted)
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
- Current: ollama/rvc/kokoro=soft | comfyui(3)/open-webui(4)/styletts2(4)/f5-tts(5)=hard

---

## UI — 6-Tab Fluid Grid
**LIVE** — mode select, kill log, services (start/stop/auto-restore/limit-mode toggle), RAM tier, enforcement card, benchmark card
**CONFIG** — LiteGraph node editor with service nodes, I/O ports, graph persistence, add-service modal, pull-model UI, right-click context menu, canvas nav with hamburger collapse
**ADVANCED** — storage paths, routing URLs, templates
**API** — How It Works guide, live topology (modes/nodes/connections, 5s auto-refresh), auto-generated endpoint reference from OpenAPI, live request monitor (stats + WS stream)
**SETTINGS** — theme, layout, background image, text/button sizing, behavior, workflow saves, data export/import/reset
**HELP** — TBD placeholder

---

## Parked Work (do not implement unless asked)
- **RAM tier** — end-to-end verification; CONFIG/template integration
- ~~**WS reconnect**~~ — DONE: auto-reconnect with 3s retry + "CONNECTION LOST" banner on all WS (status, fleet, m3)
- **boot_with_system** — oaio boot sequence should respect the flag
- **f5-tts OOM** — system RAM pressure on startup, no enforcer coverage for host RAM OOM
- ~~**Benchmark card -> service nodes**~~ — DONE: per-service VRAM/RAM estimates in node sparklines, title bar color syncs from WS
- **Security** — ports bind 0.0.0.0, no auth; plan: Tailscale + bind 127.0.0.1 + API token
- **oAudio /convert + /clone** — untested end-to-end (/speak verified)
- **oaio-workflows/** — per-disk discovery anchor + performance-profiled workflow export
- **Untouched files** — rvc_proxy.py, ram_tier.py, styletts2/app.py, extension UIs

---

## Git / GitHub
- Repo: https://github.com/zabyxc01/oAIo (public)
- Branch: main
- `.env` excluded from git (secrets) — copy `.env.example` to start
- `config/active_modes.json` excluded (runtime state)
- `templates/*.json` excluded (user runtime data)
