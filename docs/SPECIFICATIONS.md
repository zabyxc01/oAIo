# oAIo — Specifications

> Version: 0.9.0-alpha | Updated: 2026-03-07

---

## 1. Overview

oAIo is a local AI workstation orchestration platform. It manages 8 Docker containers as a unified system with shared GPU/RAM constraints, reactive resource enforcement, and a single-page web UI.

**Tagline:** OctoPrint for AI infrastructure.

---

## 2. System Requirements

### Minimum
- CPU: 8 cores (AMD or Intel)
- RAM: 32 GB
- GPU: AMD ROCm-capable (gfx900+) or NVIDIA CUDA-capable
- Storage: 100 GB free (NVMe recommended)
- OS: Ubuntu 22.04+ / Debian 12+
- Docker Engine 24+ with Compose v2

### Reference Hardware (SYS-PANDORA-OAO)
- CPU: AMD Ryzen 9 3900X (12C/24T)
- RAM: 62 GB DDR4
- GPU: AMD Radeon RX 7900 XT (20 GB VRAM, gfx1100, RDNA3)
- Storage: NVMe (OS + AI models) + SATA (Ollama models) + optional external
- ROCm 6.2, Python 3.10

---

## 3. Architecture

### 3.1 Container Topology

```
                          ┌─────────────────────────────────────────┐
                          │            oaio (Control)               │
                          │  port 9000: oLLMo API + frontend       │
                          │  port 8002: oAudio API                 │
                          │  docker.sock + /dev/dri + /mnt/oaio    │
                          └──────┬──────────────┬───────────────────┘
                                 │              │
            ┌────────────────────┼──────────────┼────────────────────┐
            │                    │              │                    │
     ┌──────┴──────┐    ┌───────┴──────┐  ┌────┴─────┐    ┌────────┴───────┐
     │   oLLM      │    │   oAudio     │  │  Render  │    │   Utility      │
     │             │    │              │  │          │    │                │
     │ ollama:11434│    │ kokoro:8000  │  │comfy:8188│    │ rustdesk       │
     │ webui:3000  │    │ rvc:8001    │  └──────────┘    │ ttyd           │
     └─────────────┘    │ f5-tts:7860 │                   └────────────────┘
                        │ style:7870  │
                        └─────────────┘
```

### 3.2 Service Registry

| Service | Container | Port | Group | Memory Mode | VRAM Est | Priority | Limit |
|---------|-----------|------|-------|-------------|----------|----------|-------|
| ollama | ollama | 11434 | oLLM | vram | 7.5 GB | 1 | soft |
| open-webui | open-webui | 3000 | oLLM | ram | 0 GB | 4 | hard |
| kokoro-tts | kokoro-tts | 8000 | oAudio | ram | 0 GB | 2 | hard |
| rvc | rvc | 8001 | oAudio | vram | 3.0 GB | 2 | hard |
| f5-tts | f5-tts | 7860 | oAudio | vram | 2.0 GB | 5 | hard |
| styletts2 | styletts2 | 7870 | oAudio | vram | 1.5 GB | 4 | hard |
| comfyui | comfyui | 8188 | Render | vram | 12.0 GB | 3 | hard |

Config: `config/services.json`

### 3.3 Activation Modes

| Mode | Services | VRAM Budget | Description |
|------|----------|-------------|-------------|
| oLLMo | ollama, open-webui, kokoro-tts, rvc | 11 GB | LLM pipeline with voice |
| oAudio | kokoro-tts, rvc, f5-tts | 5 GB | Full audio pipeline |
| comfyui-flex | comfyui | 18 GB | Image generation |

- Only one mode active at a time (enforced by displacement logic)
- Activating a mode stops displaced services from the previous mode
- Pre-flight VRAM projection check before activation
- Per-service VRAM allocations configurable within mode budget

Config: `config/modes.json`

---

## 4. Backend

### 4.1 oLLMo API (port 9000)

**Framework:** FastAPI (async, uvicorn)
**File:** `backend/ollmo/main.py`

#### Endpoints

**System**
| Method | Path | Description |
|--------|------|-------------|
| GET | `/system/status` | Full snapshot: VRAM, GPU, RAM, accounting |
| GET | `/vram` | GPU VRAM only |
| WS | `/ws` | 1 Hz push: vram/gpu/ram/services/accounting/kill_log/modes |

**Services**
| Method | Path | Description |
|--------|------|-------------|
| POST | `/services/{name}/start` | Start container (direct) |
| POST | `/services/{name}/stop` | Stop container (background thread, 3s timeout) |
| GET | `/services/{name}/status` | Container status + RAM usage |
| GET | `/services/{name}/logs` | Container log tail |
| GET | `/services/ollama/models` | List Ollama models |
| POST | `/services/ollama/models/{name}/load` | Load model into VRAM |
| POST | `/services/ollama/models/pull` | Pull new model |
| DELETE | `/services/ollama/models/{name}` | Delete model |
| GET | `/services/rvc/models` | List RVC voice models |
| POST | `/services/rvc/models/{name}/activate` | Activate voice model |
| GET | `/services/comfyui/workflows` | List ComfyUI workflows |

**Modes**
| Method | Path | Description |
|--------|------|-------------|
| GET | `/modes` | List all modes |
| POST | `/modes` | Create mode |
| PATCH | `/modes/{name}` | Update mode |
| DELETE | `/modes/{name}` | Delete mode |
| POST | `/modes/{name}/activate` | Start mode services, register enforcer |
| POST | `/modes/{name}/deactivate` | Stop mode services |
| GET | `/modes/{name}/check` | Dry-run VRAM projection |
| POST | `/modes/{name}/reset` | Restore default allocations |
| POST | `/modes/{name}/allocations/{svc}` | Set per-service allocation |
| POST | `/modes/{name}/budget` | Set mode VRAM budget |

**Enforcement**
| Method | Path | Description |
|--------|------|-------------|
| GET | `/enforcement/status` | Kill order + kill log |
| POST | `/enforcement/enable` | Enable enforcer |
| POST | `/enforcement/disable` | Disable enforcer |
| POST | `/emergency/kill` | Kill all containers immediately |
| POST | `/enforcement/ceiling` | Set virtual VRAM ceiling |
| GET | `/enforcement/thresholds` | Get warn/hard thresholds |
| POST | `/enforcement/thresholds` | Set warn/hard thresholds |

**Configuration**
| Method | Path | Description |
|--------|------|-------------|
| GET | `/config/services` | Service registry |
| POST | `/config/services` | Register new service |
| PATCH | `/config/services/{name}` | Update service fields |
| GET/POST | `/config/paths` | Symlink management |
| POST | `/config/paths/{name}` | Repoint symlink |
| DELETE | `/config/paths/{name}` | Remove symlink |
| GET/POST | `/config/routing` | Feature URL mapping |
| GET | `/config/storage/stats` | Disk I/O stats |
| GET/POST | `/config/nodes` | LiteGraph node positions |

**Templates**
| Method | Path | Description |
|--------|------|-------------|
| GET | `/templates` | List templates |
| POST | `/templates/{name}/load` | Load template |

**Benchmark**
| Method | Path | Description |
|--------|------|-------------|
| GET | `/benchmark/history` | 5-min rolling VRAM/GPU history (300 samples) |

**API Monitor**
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/monitor/stream` | Last N logged requests |
| GET | `/api/monitor/stats` | Aggregated request stats |
| WS | `/api/monitor/ws` | Live request push |

**Extensions**
| Method | Path | Description |
|--------|------|-------------|
| GET | `/extensions` | List extensions |
| POST | `/extensions/{name}/enable\|disable` | Toggle extension |
| WS | `/extensions/fleet/ws` | Fleet orchestration |
| WS | `/extensions/debugger/ws/{container}` | Live log streaming |

#### Middleware
- `RequestLogMiddleware` — ASGI middleware, logs to rolling deque(500), pushes via WS
- `_NoCacheStatic` — serves frontend with `Cache-Control: no-store`

#### Concurrency
- Config mutations protected by `asyncio.Lock()` (services.json, modes.json)
- Docker calls in WS handler via `run_in_executor()` (non-blocking)
- Container stops via `threading.Thread` with 3s timeout
- Atomic file writes via temp + rename pattern

### 4.2 oAudio API (port 8002)

**Framework:** FastAPI (async, uvicorn)
**File:** `backend/oaudio/main.py`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/speak` | text -> Kokoro TTS -> RVC -> MP3 |
| POST | `/convert` | audio -> RVC -> MP3 |
| POST | `/clone` | ref_audio + text -> F5-TTS -> WAV |
| GET | `/voices` | List available voices |

- Whisper model pre-loaded at startup for `/clone` transcription
- UUID-based temp files prevent concurrent request corruption
- HF_HOME pointed to persistent storage via env var

### 4.3 Core Modules

| Module | File | Purpose |
|--------|------|---------|
| vram | `core/vram.py` | sysfs VRAM reader (no rocm-smi) |
| resources | `core/resources.py` | VRAM projection, system accounting, thresholds |
| enforcer | `core/enforcer.py` | Reactive OOM loop (5s poll), kill log, crash detection, recovery |
| docker_control | `core/docker_control.py` | Docker SDK wrapper, auto-reconnect client |
| paths | `core/paths.py` | Symlink management, disk I/O stats |
| extensions | `core/extensions.py` | Extension loader (manifest.json, routers, nodes) |
| ram_tier | `core/ram_tier.py` | RAM tier routing logic |

### 4.4 Resource Enforcement

```
Poll cycle (every 5s, only when a mode is active):
  1. Read VRAM via sysfs
  2. If VRAM% < WARN_THRESHOLD (85%): recovery pass
     - Restart services killed by enforcer (if auto_restore=true)
     - Skip manually stopped services
  3. If VRAM% >= HARD_THRESHOLD (95%): kill pass
     - Sort services by priority (highest number = killed first)
     - Kill until VRAM drops below threshold
     - Log kill event with timestamp, reason, VRAM snapshot
  4. Crash detection: identify containers that died outside enforcer
```

- Thresholds configurable at runtime via API (live module reference, not frozen)
- Kill log persisted to `kill_log.json` via atomic writes
- `register_manual_stop()` prevents false crash detection
- Emergency kill bypasses priority ordering

---

## 5. Frontend

### 5.1 Structure

**Shell:** `frontend/src/index.html` — 6-tab layout, declares `OLLMO_API` once
**Logic:** `frontend/src/app.js` (~4000 lines) — all tab rendering, WS, settings
**Styles:** `frontend/src/style.css` — 35 CSS custom properties

### 5.2 Tabs

| Tab | Key Features |
|-----|-------------|
| LIVE | Mode selector, VRAM/GPU/RAM bars, enforcement card, kill log, service grid, benchmark canvas |
| CONFIG | LiteGraph node editor, service nodes with I/O ports, mode group boxes, add-service modal, pull-model UI |
| ADVANCED | Storage paths (tier badges, repoint UI), routing URLs, template load |
| API | How It Works guide, live topology diagram (5s refresh), OpenAPI endpoint reference, request monitor |
| SETTINGS | Theme (35 color vars), layout, background image, text sizing, behavior toggles, data export/import |
| HELP | Placeholder |

### 5.3 Color System

35 CSS custom properties organized in 5 groups:

**Storage Tiers** (accent + dark background)
| Var | Value | Use |
|-----|-------|-----|
| `--tier-nvme` / `--tier-nvme-bg` | `#2196f3` / `#0d1a2f` | NVMe storage |
| `--tier-sata` / `--tier-sata-bg` | `#ffa726` / `#2a1e00` | SATA storage |
| `--tier-ram` / `--tier-ram-bg` | `#00e676` / `#0a2a14` | RAM tier |
| `--tier-vram` / `--tier-vram-bg` | `#ab47bc` / `#1f0d2a` | VRAM |

**Service Groups** (node coloring at 12% tint)
| Var | Value | Use |
|-----|-------|-----|
| `--grp-llm` | `#42a5f5` | oLLM group nodes |
| `--grp-audio` | `#ffa726` | oAudio group nodes |
| `--grp-render` | `#66bb6a` | Render group nodes |
| `--grp-control` | `#78909c` | Control group nodes |

**Modes**
| Var | Value |
|-----|-------|
| `--mode-ollmo` | `#e879f9` |
| `--mode-oaudio` | `#22d3ee` |
| `--mode-comfyui` | `#facc15` |

**Status**
| Var | Value |
|-----|-------|
| `--green` | `#00e676` |
| `--yellow` | `#ffd740` |
| `--red` | `#ff1744` |
| `--cyan` | `#00d2be` |
| `--purple` | `#a855f7` |

**Base**
| Var | Default |
|-----|---------|
| `--bg1` | `#0a0a0a` |
| `--bg2` | `#0f0f0f` |
| `--bg3` | `#161616` |
| `--border` | `#252525` |
| `--text` | `#e0e0e0` |
| `--text-dim` | `#555` |

All colors user-customizable in SETTINGS tab. All JS fallbacks match these defaults.

### 5.4 LiteGraph Integration

- Lazy-loaded (~1 MB) only when CONFIG tab clicked
- Service nodes registered dynamically from `config/services.json`
- Tier 3 capability sub-nodes: LLM models, workflows, voice models, TTS voices
- Mode group boxes drawn as canvas overlays with mode-colored borders
- Node positions persisted via `GET/POST /config/nodes`
- Extension nodes queued via `window._pendingExtNodes`

### 5.5 Security (Frontend)

- `_esc()` HTML escape function applied to all user-controllable data in innerHTML (~40 call sites)
- Covers: mode names, service names, paths, routing values, topology display, discovery scanner, error messages

---

## 6. Symlink Bus

16 symlinks under `/mnt/oaio/` abstract all service data paths. Repointing a symlink instantly reroutes a service's data between storage tiers with zero downtime.

| Symlink | Default Target | Services |
|---------|---------------|----------|
| ollama | /mnt/windows-sata/ollama-models | ollama |
| models | /mnt/storage/ai/comfyui/models | comfyui |
| lora | /mnt/storage/ai/comfyui/models/loras | comfyui |
| custom-nodes | ~/ComfyUI/custom_nodes | comfyui |
| comfyui-user | ~/ComfyUI/user | comfyui |
| outputs | ~/ComfyUI/output | comfyui |
| inputs | ~/ComfyUI/input | comfyui |
| audio | /mnt/storage/ai/audio | rvc |
| kokoro-voices | /mnt/storage/ai/audio/kokoro-voices | kokoro-tts |
| hf-cache | /mnt/storage/ai/audio/huggingface | f5-tts, styletts2, comfyui, oaio |
| ref-audio | ~/reference-audio | oaio |
| rvc-ref | ~/Videos/audio/_EDITED | oaio |
| swap | /mnt/storage/swap | — |
| training | /mnt/storage/ai/training | — |
| rvc-weights | /mnt/storage/ai/audio/rvc-weights | rvc |
| rvc-indices | /mnt/storage/ai/audio/rvc-indices | rvc |

**Repoint API:** `POST /config/paths/{name}` with `{"target": "/new/path"}` or shortcut `{"target": "ram"}` / `{"target": "default"}`

**Safety:** Link path validated under SYMLINK_ROOT, target must be absolute with no `..`, target dirs auto-created if missing.

---

## 7. Extension System

Extensions live in `extensions/<name>/` with:
- `manifest.json` — name, version, description, enabled flag
- `backend.py` — FastAPI APIRouter (auto-mounted at `/extensions/<name>`)
- `nodes.js` (optional) — LiteGraph node definitions

### Installed Extensions

| Extension | Description | Endpoints |
|-----------|-------------|-----------|
| fleet | Multi-node orchestration (hub-and-spoke) | WS `/extensions/fleet/ws`, REST for node/job management |
| debugger | Live container log streaming | WS `/extensions/debugger/ws/{container}` |
| example | Template extension | — |

Fleet SSRF protection: `_is_safe_url()` validates node URLs against private IP blocklist at registration.

---

## 8. Docker Compose

**File:** `docker-compose.yml`
**Network:** `oaio-net` (bridge)

### Build vs Pull
| Build (local Dockerfile) | Pull (remote image) |
|--------------------------|---------------------|
| oaio, comfyui, styletts2 | ollama, open-webui, kokoro-tts, rvc, f5-tts |

### Key Volume Mounts (all via /mnt/oaio/)
- `oaio`: docker.sock, /dev/dri, /dev/kfd, sysfs GPU, /mnt/oaio, config, extensions
- `ollama`: /mnt/oaio/ollama -> container /root/.ollama
- `comfyui`: models, custom-nodes, outputs, inputs, user dirs
- Audio services: kokoro-voices, rvc-weights, rvc-indices, hf-cache, ref-audio

### Environment
- `HF_HOME=/mnt/oaio/hf-cache` (oaio container)
- Open WebUI RAG: `RAG_EMBEDDING_ENGINE=ollama`, `RAG_EMBEDDING_MODEL=nomic-embed-text`
- GPU: `--device /dev/kfd --device /dev/dri --group-add video` (ROCm services)

---

## 9. Tooling

| Script | Lines | Purpose | Flags |
|--------|-------|---------|-------|
| `install.sh` | 974 | Interactive installer: sys requirements, GPU detect, component select, symlinks, build, compose up, model pulling, systemd | — |
| `update.sh` | 611 | Backup configs, pull images, rebuild, rolling restart, heal symlinks, health check | `--yes` (skip prompts) |
| `repair.sh` | 1107 | Diagnose, fix symlinks, validate configs, Docker cleanup, HF cache dedup, stray model finder | `--check` (dry-run) |
| `uninstall.sh` | 345 | 7-step interactive teardown: containers, images, symlinks, systemd, .env, config, volumes | — |

All scripts share consistent color helpers, `need_sudo`/`run_root` pattern, and interactive y/N prompts. Never delete model data.

---

## 10. Security

### Implemented
- HTML escaping (`_esc()`) on all user-data in innerHTML
- Path traversal protection on template load/save, file upload, symlink repoint
- Container name regex validation (`^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`)
- Fleet SSRF protection (private IP blocklist)
- Config mutation locking (`asyncio.Lock`)
- Docker client auto-reconnect with `ping()` check
- Atomic file writes (temp + rename)

### Not Yet Implemented
- Authentication / API tokens
- TLS termination
- Port binding to localhost (currently 0.0.0.0)
- Rate limiting
- Audit logging

Current mitigation: Tailscale network boundary (utility services bind to Tailscale IP only).

---

## 11. Configuration Files

| File | Purpose | Key Fields |
|------|---------|------------|
| `config/services.json` | Service registry | container, port, group, memory_mode, vram_est_gb, priority, limit_mode, auto_restore, capabilities |
| `config/modes.json` | Activation modes | services[], vram_budget_gb, allocations{}, description |
| `config/paths.json` | Symlink definitions | link, default_target, label, containers[] |
| `config/routing.json` | Feature URL mapping | key-value pairs |
| `config/nodes.json` | LiteGraph positions | nodeConfigs, modeConfigs |
| `config/profiles.json` | Saved profiles | — |
| `config/active_modes.json` | Runtime state (gitignored) | — |

---

## 12. Known Limitations

- Single GPU only (no multi-GPU scheduling)
- No authentication (relies on network boundary)
- oAudio `/convert` and `/clone` untested end-to-end
- RAM tier routing not verified end-to-end
- Benchmark card not wired to service nodes
- WS has no reconnect logic on disconnect
- `boot_with_system` flag not enforced at startup
- Host RAM OOM not covered by enforcer (only VRAM)
- No Windows/macOS support (Linux sysfs dependency)
