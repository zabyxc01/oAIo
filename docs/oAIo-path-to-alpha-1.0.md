# oAIo — Path to Alpha 1.0
> This document is both a roadmap and a cold-start context prompt.
> If resuming work on this project, read this first.

---

## System — SYS-PANDORA-OAO
- **CPU:** AMD Ryzen 9 3900X | **RAM:** 62GB
- **GPU:** RX 7900 XT 20GB (gfx1100, RDNA3) — ROCm 6.2
- **OS:** Ubuntu 22.04.5 LTS | Python 3.10
- **Storage:**
  - `/` (nvme0n1p1) — OS, ComfyUI native install
  - `/mnt/storage` (nvme0n1p3) — AI models, oAIo project
  - `/mnt/windows-sata` (sda1) — Ollama models (49GB)
  - `/media/oao/My Passport` — 2.7TB external, Obsidian vault
- **VRAM monitoring:** sysfs only — `/sys/class/drm/card1/device/mem_info_vram_used|total|gpu_busy_percent` — NO rocm-smi
- Always use `python3`. Prefer Docker over system pip installs.

---

## Project Location
```
/mnt/storage/oAIo/          ← git repo root
  backend/
    ollmo/main.py           ← oLLMo API (port 9000) — primary control plane
    oaudio/main.py          ← oAudio API (port 8002) — unified voice pipeline
    core/
      vram.py               ← sysfs VRAM reader
      resources.py          ← VRAM projection + OOM alerts + system accounting
      enforcer.py           ← reactive OOM loop (background asyncio task)
                               register_manual_stop() — prevents crash-detection restart on manual stop
      paths.py              ← symlink management + storage stats
      docker_control.py     ← Docker SDK wrapper (stop timeout=3, non-blocking)
      extensions.py         ← scans extensions/ at startup, loads FastAPI routers
  config/
    services.json           ← 6 managed services with VRAM/priority/limit_mode/auto_restore
    modes.json              ← 7 activation modes with budgets + allocations
    paths.json              ← 14 symlink entries (all green)
    routing.json            ← feature URL mapping (tts, ollama, imggen, stt)
    active_modes.json       ← persisted active modes — survives restarts (auto-generated)
  frontend/src/
    index.html              ← 3-tab shell (LIVE/CONFIG/ADVANCED), OLLMO_API declared ONCE
    app.js                  ← tab switching, WS data → cards, mode confirm flow
    panels/timeline.js      ← rolling 120s heatmap (VRAM + RAM + GPU + NVMe + SATA)
    extensions-loader.js    ← injects extension JS/CSS; queues node scripts for lazy load
    litegraph.js / .css     ← lazy-loaded only when CONFIG tab clicked
    nodes/services.js       ← LiteGraph service node definitions
    nodes/capabilities.js   ← sub-graph capability nodes
    style.css               ← all styles
  extensions/
    fleet/                  ← multi-node orchestration (enabled)
    debugger/               ← live container log streaming (enabled)
    example/                ← reference extension (disabled)
  docker/
    oaio/Dockerfile
    oaio/start.sh           ← starts both uvicorn processes
    comfyui/Dockerfile
  docker-compose.yml        ← all 7 containers, all volumes via /mnt/oaio/*
  install.sh                ← system requirements check + symlinks + systemd
  docs/
    oAIo-path-to-alpha-1.0.md ← this file (ignition prompt)
```

---

## What oAIo Is
The nervous system for a local AI workstation. Not an AI tool — the layer that makes all AI tools aware of each other, enforces shared resource constraints, and presents them as one coherent system instead of 7 disconnected Docker containers.

**The pitch:** OctoPrint for AI infrastructure.

**The shape we fill:** None of these tools talk to each other. ComfyUI doesn't know Ollama exists. RVC doesn't know ComfyUI is eating its VRAM. oAIo is the gap between "I installed a bunch of AI tools" and "I have an AI workstation."

---

## The Docker Stack
| Container | Image | Port(s) | Notes |
|---|---|---|---|
| ollama | ollama/ollama:rocm | 11434 | /mnt/oaio/ollama → 49GB SATA |
| open-webui | ghcr.io/open-webui/open-webui:main | 3000→8080 | TTS→http://rvc:8001/v1 |
| kokoro-tts | kokoro-tts (local) | 8000 | /mnt/oaio/kokoro-voices |
| f5-tts | f5-tts (local) | 7860 | Gradio API at /gradio_api/ |
| rvc | rvc (local) | 7865, 8001 | Gradio at 7865, proxy at 8001 |
| comfyui | comfyui (local) | 8188 | /mnt/oaio/models (NVMe) |
| oaio | oaio (local) | 9000, 8002 | docker.sock + /sys/class/drm + /mnt/oaio |

**Run:** `cd /mnt/storage/oAIo && docker compose up -d`

---

## Symlink Layer — /mnt/oaio/ (all 14 verified green)
| Link | Target | Tier |
|---|---|---|
| ollama | /mnt/windows-sata/ollama-models | SATA |
| models | /mnt/storage/ai/comfyui/models | NVMe |
| lora | /mnt/storage/ai/comfyui/models/loras | NVMe |
| custom-nodes | /home/oao/ComfyUI/custom_nodes | NVMe |
| comfyui-user | /home/oao/ComfyUI/user | NVMe |
| outputs | /home/oao/ComfyUI/output | NVMe |
| inputs | /home/oao/ComfyUI/input | NVMe |
| audio | /mnt/storage/ai/audio | NVMe |
| kokoro-voices | /mnt/storage/ai/audio/kokoro-voices | NVMe |
| hf-cache | /mnt/storage/ai/audio/huggingface | NVMe |
| ref-audio | /home/oao/reference-audio | NVMe |
| rvc-ref | /home/oao/Videos/audio/_EDITED | NVMe |
| swap | /mnt/storage/swap | NVMe |
| training | /mnt/storage/ai/training | NVMe |

All volumes in docker-compose.yml go through `/mnt/oaio/*` — zero hardcoded personal paths.

---

## API Endpoints (port 9000)
```
GET  /system/status           VRAM/GPU/RAM/services/alerts
GET  /vram                    raw VRAM only (fast, used for healthcheck)
GET  /enforcement/status      kill order, active modes, paused state

GET  /services                list all services
POST /services/{name}/start
POST /services/{name}/stop
GET  /services/{name}/status
GET  /services/{name}/logs

GET  /modes                   list all modes
GET  /modes/{name}/check      pre-flight VRAM check
POST /modes/{name}/activate   blocks if projected >= 95% VRAM
POST /modes/{name}/deactivate tells enforcer mode is off
GET  /modes/{name}/allocations
POST /modes/{name}/allocations/{service}
POST /modes/{name}/budget

GET  /services/ollama/models
POST /services/ollama/models/{name}/load
GET  /services/rvc/models
POST /services/rvc/models/{name}/activate
GET  /services/comfyui/workflows

GET  /config/paths            all symlinks with exists/tier/containers
POST /config/paths            add new path entry + create symlink
POST /config/paths/{name}     repoint existing symlink
DELETE /config/paths/{name}   remove path entry + symlink
GET  /config/routing
POST /config/routing
GET  /config/storage/stats

GET  /templates
POST /templates/save
POST /templates/{name}/load
```

## oAudio Endpoints (port 8002)
```
GET  /status
GET  /voices
POST /speak       text → Kokoro → RVC → MP3
POST /convert     audio file → RVC infer_convert → WAV
POST /clone       ref audio + text → F5-TTS basic_tts → WAV
```

---

## Resource Enforcement
- **enforcer.py** polls every 5s
- **ONLY enforces when at least one mode is active** — safe during gaming, idle, anything else
- Kill order: highest priority number first (5=first to die, 1=protected)
- `limit_mode: soft` = warn only, never kill | `limit_mode: hard` = auto-kill on OOM
- `auto_restore: true/false` per service — controls whether enforcer restarts after kill
- `register_manual_stop(ctr, svc_name, priority)` — call on manual stop so crash detection ignores it
- Recovery skips `reason="manual"` and `auto_restore=false` entries
- Stop endpoint: background thread + timeout=3s — instant UI response, 3s max SIGKILL
- Cooldown: won't kill same container twice in a row
- Current: ollama/rvc/kokoro=soft | comfyui(3)/open-webui(4)/f5-tts(5)=hard
- WARN at 85% (17GB) | HARD at 95% (19GB)
- Kill log: last 50 events (kill/restore/crash) in WS stream + `/enforcement/status`

---

## Ollama Models (SATA, ~13s load)
- wizard-vicuna-uncensored:13b (7.4GB)
- llama3.1:8b (4.9GB)
- qwen2.5:14b (9.0GB)
- dolphin-mixtral:8x7b (26.4GB)
- deepseek-coder:6.7b (3.8GB)

---

## Critical Rules (Never Break)
- NEVER delete native installs — ComfyUI at ~/ComfyUI/ stays
- Do NOT modify /etc/fstab, PipeWire, ROCm, systemd without explicit user approval
- `sudo` always requires user approval
- `OLLMO_API` declared ONCE in index.html as `window.location.origin`
- sysfs only for VRAM — no rocm-smi
- Enforcement loop is mode-aware — pauses when no mode active
- All volumes go through `/mnt/oaio/*` — zero hardcoded personal paths in compose
- ComfyUI models on NVMe (bus speed), Ollama models on SATA (load-once, size)

---

## Current % — Alpha 1.0 Progress
| Area | % | Notes |
|---|---|---|
| Backend/API | 90% | Enforcement, paths, voice pipeline, extensions, WS all working |
| Frontend/UI | 70% | 3-tab fluid grid done; CONFIG/nodes parked; ADVANCED unverified |
| Infrastructure | 90% | Compose portable, symlinks solid, .env + install script exist |
| Product/Design | 75% | Vision clear, architecture defined, template/node system parked |
| **Overall** | **80%** | |

---

## Completed ✅
- Data-driven service registration (`services.json` capabilities block) ✅
- WebSocket 1Hz push stream (replaces polling) ✅
- `.env` file + install script ✅
- Extension system (`extensions/` manifest, fleet, debugger) ✅
- 3-tab fluid grid UI (LIVE / CONFIG placeholder / ADVANCED) ✅
- LIVE cards: mode select, kill log, services (auto-restore toggle), RAM tier, timeline ✅
- Enforcer: kill log, recovery, crash watch, manual-stop awareness, auto_restore ✅
- Stop latency fix: background thread + 3s SIGKILL ✅
- Timeline: resizable heatmap, all rows off by default, user selects views ✅
- `PATCH /config/services/{name}` endpoint ✅

## Parked — Next Build

### CONFIG Tab (umbrella for all planning features)
- [ ] LiteGraph node graph visible in CONFIG tab
- [ ] Templates — define boot order + workload sequence via nodes, save/load
- [ ] RAM tier per-template (nodes define which paths are RAM-pinned per session)
- [ ] Accounting card in CONFIG tab (VRAM/RAM estimation for planned workloads)

### RAM Tier
- [ ] LIVE card: verified end-to-end (symlink flip + pool tracking working)
- [ ] CONFIG: programmable per template

### Infrastructure
- [ ] Install script: with/without UI option (headless/server profile)
- [ ] ADVANCED tab: verify routing form + paths editor save correctly
- [ ] WS reconnect: clean recovery if connection drops
- [ ] boot_with_system: oaio boot sequence respects the flag (currently docker compose handles it)
- [ ] f5-tts OOM on startup — system RAM pressure, no enforcer coverage for host RAM OOM

### Install Script — Two Step Flow (design unchanged)

**Step 1 — Deployment type (sets context, filters options, sets defaults)**
```
( ) Local Workstation    — daily driver, full control
( ) Training Node        — rented GPU, temporary, job-focused
( ) Fleet Node           — managed instance, remotely orchestrated
( ) Custom               — I know what I'm doing
```

**Step 2 — Component selection (filtered by Step 1, pull only what's selected)**
```
[x] Control Plane        — oAIo core (always required)
[ ] LLM Stack            — Ollama, Open-WebUI        (~8GB)
[ ] Voice Stack          — Kokoro, RVC, F5-TTS       (~4GB)
[ ] Render Stack         — ComfyUI                   (~6GB)
[ ] Training Stack       — Kohya, Axolotl            (~5GB)
[ ] Fleet Services       — instance registry, LB     (~1GB)
```

**How Step 1 affects Step 2:**
- Local Workstation → everything available, nothing pre-checked except Control Plane
- Training Node → only Training Stack + Control Plane available
- Fleet Node → Fleet Services pre-checked, others optional
- Custom → everything available, nothing pre-checked

**Step 3 — Pull only selected components**
- No downloading images you don't need
- Detects GPU vendor/arch, writes `.env`
- Creates symlinks for selected stacks only
- `docker compose up -d` with filtered profile
- Prints: `oAIo running at http://localhost:9000`

**In the UI — same flow in Config page**
- Deployment Profile section mirrors install choices
- Check/uncheck service groups → hit Apply → oAIo handles spin up/down
- Run install again on existing system to add components without touching what's running

### Docs Structure (generated from config)
```
docs/stack/
  profiles/
    local.md             ← Local Workstation profile
    training-node.md     ← Training Node profile
    fleet-node.md        ← Fleet Node profile
  types/                 ← one file per service type (generated)
    ollama.md
    comfyui.md
    rvc.md
    kokoro-tts.md
    f5-tts.md
    open-webui.md
    oaio.md
  instances/             ← one file per running instance (generated)
```

### Fleet Architecture (future, same codebase)
- Node type → N instances, each with own dataset/config/port
- Instance registry — named instances, auto port allocation
- Load balancer routing — `/speak` hits least-loaded RVC instance
- Central oAIo (Local) orchestrates remote Fleet Nodes via RunPod API
- Fleet-of-one locally → fleet-of-N remotely, same interface

### Final
- [ ] Install script (two-step flow above)
- [ ] Git tag v0.1.0-alpha

---

## UI Design Principles
- **Fluid grid** — not rigid panels, reflows to any screen
- **Mobile-first** — phone is a remote control (OctoPrint model)
- Topbar gauges = what IS happening (always visible, all views)
- Heatmap = what SHOULD be happening (mode predictions, not live data mirror)
- Detailed performance view = per-process breakdown, event log (click to expand)
- Node graph lives in Planner page, not the main view

---

## Remote / Headless Use Case
```bash
# SSH tunnel — full UI in browser, feels local
ssh -L 9000:localhost:9000 user@remote-box
# → browser: localhost:9000

# One command install on rented GPU box
curl -sSL https://raw.githubusercontent.com/zabyxc01/oAIo/main/install.sh | bash

# Training workflow
# 1. Drop datasets → /mnt/oaio/swap
# 2. Move to /mnt/oaio/training via paths panel
# 3. Spin up Kohya/Axolotl via mode
# 4. Monitor on Live page
# 5. Pull weights to swap
# 6. Destroy instance
```

---

## Services Worth Adding (Post-Alpha)

### LLM Support Layer
- Faster-Whisper — STT (fills empty stt_url in routing.json)
- ChromaDB / Qdrant — vector store, persistent memory
- SearXNG — private search LLM can query
- Infinity — dedicated embedding server

### oAudio
- AllTalk TTS / Bark / MeloTTS — more voice options
- MusicGen / AudioCraft / Stable Audio — text to music
- WhisperX — real-time STT pipeline

### Render
- Automatic1111 / Forge / InvokeAI — SD frontends
- Wan2.1 / LTX-Video — video generation
- TripoSR / InstantMesh — image to 3D
- Kohya SS — LoRA training | Axolotl — LLM fine-tuning

### oAgent (new group)
- n8n — workflow automation
- Open Interpreter — code execution, desktop control
- Flowise — visual LLM workflow builder
- SillyTavern — character/persona frontend

---

## The Unstoppable Factor
Extension system + install script together = the moment oAIo stops being a personal tool and becomes a platform other people build on. Community ships `oaio-extension.json` + a Docker image. You drop the manifest in, the service appears. No code changes required.

---

## GitHub
https://github.com/zabyxc01/oAIo
