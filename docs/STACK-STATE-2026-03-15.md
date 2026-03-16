---
name: oAIo + Desktop Waifu — Full Stack State
description: Definitive current-state reference for the oAIo Docker orchestration platform and desktop-waifu Electron companion app. Line counts, file inventories, service configs, symlink bus, container states, and what works vs what doesn't.
type: project
---

# oAIo + Desktop Waifu — Full Stack State (2026-03-15)

## System — SYS-PANDORA-OAO
- CPU: Ryzen 9 3900X (12C/24T) | RAM: 62GB DDR4 | GPU: RX 7900 XT 20GB (gfx1100, RDNA3)
- OS: Ubuntu 24.04 LTS | KDE Plasma 5.27 | X11 (NOT Wayland)
- Kernel: 6.17.0-14-generic | Mesa 25.2.8
- ROCm: Docker only (never on host)
- Monitor: Dell AW3425DW 3440x1440 ultrawide

## Storage
| Mount | Device | Size | Used | Purpose |
|-------|--------|------|------|---------|
| `/` | nvme0n1p1 | 596G | 450G (80%) | OS, Steam, home |
| `/mnt/storage` | nvme0n1p3 | 319G | 171G (57%) | Fast staging, NVMe I/O |
| `/mnt/windows-sata` | sda1 | 220G | 116G (56%) | oAIo data, ollama models |
| `/dev/shm` | tmpfs | 32G | 922M | RAM tier (whisper cache) |

---

## oAIo — /mnt/windows-sata/oAIo/

**GitHub:** https://github.com/zabyxc01/oAIo (public)
**Purpose:** Local AI workstation orchestration — 10 Docker containers as one system.

### Source Files (10,637 lines backend+frontend)

| File | Lines | Purpose |
|------|-------|---------|
| `backend/ollmo/main.py` | 1992 | Control plane API (port 9000) — FastAPI, WS 1Hz push, mode management, service control, config CRUD, VRAM/GPU monitoring, enforcer control, Ollama model management, ComfyUI workflow listing, extension loading, request logging, benchmark history |
| `backend/oaudio/main.py` | 277 | Voice pipeline API (port 8002) — /speak (Kokoro→RVC→MP3), /convert (audio→RVC), /clone (F5-TTS), /voices |
| `backend/core/enforcer.py` | 291 | Reactive OOM loop — polls VRAM+RAM every 5s, kills lowest-priority hard-limit containers at 95%, recovers at <85%, crash watch with backoff, manual stop awareness |
| `backend/core/resources.py` | 187 | VRAM budgeting — pre-activation projection, warn/hard thresholds (85%/95%), virtual ceiling support |
| `backend/core/paths.py` | 312 | Symlink bus management — create/repoint/delete symlinks, tier inference, heal dangling, storage stats |
| `backend/core/vram.py` | 45 | sysfs VRAM reader — reads /sys/class/drm/cardN/device/mem_info_vram_*, gpu_busy_percent |
| `backend/core/docker_control.py` | 142 | Docker SDK wrapper — get_status, start/stop with timeout=3, background thread stops, auto-reconnect on stale client |
| `backend/core/extensions.py` | 125 | Extension loader — scans extensions/ for manifest.json, mounts FastAPI routers, registers frontend assets |
| `frontend/src/app.js` | 5524 | Full UI — 6-tab shell, WS live cards, mode confirm flow, LiteGraph config editor, API monitor, settings system, extension UIs (fleet, debugger, m3) |
| `frontend/src/index.html` | 1691 | HTML shell — 6 tabs (LIVE, CONFIG, ADVANCED, API, SETTINGS, HELP), all element IDs, OLLMO_API declared once |
| `frontend/src/extensions-loader.js` | 51 | Queues extension node scripts for lazy load with LiteGraph |

### Docker Stack (10 containers)

| Container | Image | Ports | GPU | Status | Purpose |
|-----------|-------|-------|-----|--------|---------|
| ollama | ollama/ollama:rocm | 11434 | ROCm | Running | LLM inference |
| open-webui | ghcr.io/open-webui/open-webui:main | 3000 | — | Running | Chat UI + RAG |
| kokoro-tts | kokoro-tts (local) | 8000 | — | Running | TTS synthesis (ONNX, CPU) |
| rvc | rvc:fixed (local) | 7865, 8001 | ROCm | Exited | Voice conversion proxy |
| f5-tts | f5-tts:fixed (local) | 7860 | ROCm | Exited | Voice cloning |
| styletts2 | oaio-styletts2 (local) | 7870 | ROCm | Exited | Voice prototyping |
| faster-whisper | oaio-faster-whisper (local) | 8003, 7880 | — (CPU) | Running | STT (CTranslate2, medium model) |
| comfyui | oaio-comfyui (local) | 8188 | ROCm | Exited | Image gen (Flux.1-dev) |
| indextts | oaio-indextts (local) | 8004, 7890 | ROCm | Running | IndexTTS-2 voice cloning |
| oaio | oaio-oaio (local) | 9000, 8002 | ROCm | Running | Control plane + oAudio |

All ports bind to `${OAIO_BIND:-127.0.0.1}` (localhost only by default).
All volumes route through `/mnt/oaio/*` symlink bus.

### Operational Modes (6 defined)

| Mode | ID | Services | VRAM Budget |
|------|----|----------|-------------|
| oLLMo | 1 | ollama, open-webui, kokoro-tts, rvc | 11 GB |
| oAudio | 2 | kokoro-tts, rvc, f5-tts | 5 GB |
| comfyui-flex | 3 | comfyui | 18 GB |
| Waifu | 4 | ollama, kokoro-tts, rvc, faster-whisper | 11 GB |
| IndexTTS | 5 | indextts | 8 GB |
| oAudio Optimized | 6 | ollama, faster-whisper, indextts | 16 GB |

### Services Config (9 registered)

| Service | VRAM Est | RAM Est | Priority | Limit | Group |
|---------|----------|---------|----------|-------|-------|
| ollama | 7.5 GB | 7.2 GB | 5 | hard | oLLM |
| open-webui | 0 | 1.3 GB | 30 | hard | oLLM |
| kokoro-tts | 0 | 0.8 GB | 10 | hard | oAudio |
| rvc | 3.0 GB | 6.0 GB | 15 | hard | oAudio |
| f5-tts | 2.0 GB | 1.4 GB | 40 | hard | oAudio |
| comfyui | 12.0 GB | 4.0 GB | 25 | hard | Render |
| faster-whisper | 0 | 2.0 GB | 12 | hard | oAudio |
| styletts2 | 1.5 GB | 2.0 GB | 35 | hard | oAudio |
| indextts | 8.0 GB | 4.0 GB | 45 | hard | oAudio |

### Symlink Bus — /mnt/oaio/ (20 entries, all healthy)

All symlinks resolve. Staging dirs on NVMe exist.
Key paths: ollama→SATA, models→SATA, hf-cache→SATA, staging/*→NVMe.

### Dockerfiles (7, total 254 lines)

| Dockerfile | Lines | Base |
|------------|-------|------|
| comfyui | 100 | ROCm base |
| indextts | 42 | ROCm base |
| styletts2 | 38 | ROCm base |
| oaio | 30 | Python slim |
| faster-whisper | 28 | Python slim (CPU) |
| rvc | 8 | Minimal |
| f5-tts | 8 | Minimal |

### Scripts (3,238 lines total)

| Script | Lines | Purpose |
|--------|-------|---------|
| install.sh | 1120 | Interactive installer — Docker, symlinks, model pull, systemd |
| repair.sh | 1107 | Diagnose + fix symlinks, Docker cleanup, HF dedup |
| update.sh | 611 | Backup + pull + rebuild + rolling restart + heal |
| uninstall.sh | 345 | Interactive 7-step uninstaller (never deletes model data) |
| scripts/setup-oaio-symlinks.sh | 55 | Create /mnt/oaio symlink bus |

### Extensions (4)

| Extension | Files | State |
|-----------|-------|-------|
| fleet | backend.py, nodes.js, manifest.json, fleet.json | Enabled, WS auto-reconnect, multi-node discovery UI |
| debugger | backend.py, nodes.js, manifest.json | Enabled, live log streaming per container |
| m3 | backend.py, nodes.js, manifest.json, m3.json | Enabled, multi-model pipeline orchestration |
| example | backend.py, manifest.json | Template for new extensions |

### Key Architecture

- WS 1Hz push — `/ws` streams vram/gpu/ram/services/accounting/kill_log/active_modes
- Auto-reconnect on all WebSockets (status, fleet, m3) with 3s retry + banner
- Static files served with `Cache-Control: no-store`
- LiteGraph lazy-loaded only when CONFIG tab clicked
- Config writes use asyncio.Lock + atomic temp+rename
- HTML escaping via `_esc()` on all innerHTML (~40 sites)
- Optional API token auth (OAIO_API_TOKEN env var, currently empty)
- Docker socket mounted for orchestration
- Enforcer pauses when no mode active (safe for gaming)

### Parked / Incomplete

- RAM tier — end-to-end verification not done
- boot_with_system — UI toggle exists, backend may not respect it fully
- f5-tts OOM — host RAM pressure on startup
- Security — auth token empty, planned: Tailscale + bind 127.0.0.1 + token
- oAudio /convert + /clone — untested e2e (pre-KDE migration they worked)
- oaio-workflows — per-disk discovery + profiled export
- Untouched files — rvc_proxy.py, ram_tier.py, styletts2/app.py
- ComfyUI models dir empty — no checkpoints/loras/vae subdirs created yet

---

## Desktop Waifu — /home/oao/desktop-waifu/

**Not yet on GitHub.** Planned repo: `zabyxc01/desktop-anime-ai` (private).
**Purpose:** Electron + three-vrm desktop AI companion with voice conversation.

### Source Files (2,166 lines)

| File | Lines | Purpose |
|------|-------|---------|
| `src/main/index.js` | 129 | Electron main process — window creation, X11 keep-below, hotkeys (Esc/F2), IPC for mouse events + view switching |
| `src/main/preload.js` | 7 | IPC bridge — setIgnoreMouse, onPTT |
| `src/renderer/index.html` | 441 | UI markup + CSS — compact/desktop layouts, chat bubbles, model pills, TTS pills, VRAM bar, settings menu |
| `src/renderer/renderer.src.js` | 667 | Three.js VRM avatar, animations (blink, breathe, head sway, walk, jumping jacks), model toolbar, VRAM monitor, view switching, settings, text input, lip sync |
| `src/renderer/voice/config.js` | 51 | All config — LLM model, TTS backend selection, endpoint URLs, persona system prompt, Open WebUI JWT |
| `src/renderer/voice/pipeline.js` | 247 | State machine (IDLE→LISTENING→PROCESSING→SPEAKING), coordinates STT→LLM→TTS, audio queue, live response bubbles |
| `src/renderer/voice/llm.js` | 143 | Ollama streaming client, sentence-level chunking, conversation history persistence to disk, 200-char threshold before splitting |
| `src/renderer/voice/tts.js` | 94 | TTS dispatcher — routes to kokoro, kokoro-rvc, or indextts based on config |
| `src/renderer/voice/stt.js` | 21 | Sends audio to faster-whisper, returns text |
| `src/renderer/voice/mic.js` | 46 | MediaRecorder capture in WebM+Opus |
| `src/renderer/voice/lipsync.js` | 81 | Web Audio API frequency analysis → VRM mouth shapes (aa, ih, ou) |
| `src/renderer/voice/openwebui-sync.js` | 239 | Bidirectional Open WebUI sync — polls every 3s, pushes waifu responses |

### Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| three | ^0.183.2 | 3D rendering |
| @pixiv/three-vrm | ^3.5.1 | VRM avatar loading |
| electron | ^41.0.2 | Desktop app shell |
| esbuild | ^0.27.4 | JS bundler |

### View Modes

| Mode | Window Size | Camera | Background |
|------|------------|--------|------------|
| Compact (default) | 850x840, bottom-right | Face/chest zoom (Y=1.35, Z=1.8) | Transparent |
| Desktop | 1200x full height, right side | Full body (Y=0.75, Z=3.5) | Transparent |

**Note:** True fullscreen (3440x1440) causes KWin compositor ghosting on X11. Desktop mode uses 1200px width as workaround. Would need Wayland/Plasma 6 for real fullscreen.

### TTS Backends

| Backend | Config Key | Service | Port | UI Pill | Status |
|---------|-----------|---------|------|---------|--------|
| Kokoro | kokoro | kokoro-tts | 8000 | Yes | Working |
| Kokoro+RVC | kokoro-rvc | oaio (8002) | 8002 | Hidden (in dropdown only) | Working but untested recently |
| IndexTTS | indextts | indextts | 8004 | Yes | Working |

### Hotkeys

| Key | Action |
|-----|--------|
| F2 | Push-to-talk toggle (global, registered in main process) |
| F3 | Toggle walking animation |
| F4 | Toggle jumping jacks |
| F5 | Toggle settings menu |
| Escape | Quit app |

### IPC Channels

| Channel | Direction | Purpose |
|---------|-----------|---------|
| voice:ptt-start | main→renderer | F2 hotkey forwarding |
| set-ignore-mouse | renderer→main | Toggle click-through |
| set-view-mode | renderer→main | Switch compact/desktop (resizes window) |

### External Service Dependencies

| Service | URL | Required For | If Down |
|---------|-----|-------------|---------|
| Ollama | 127.0.0.1:11434 | LLM responses, model toolbar | App useless, generic error |
| Kokoro TTS | 127.0.0.1:8000 | Kokoro voice synthesis | TTS fails silently |
| oAudio | 127.0.0.1:8002 | Kokoro+RVC voice | TTS fails if selected |
| IndexTTS | 127.0.0.1:8004 | IndexTTS voice cloning | TTS fails if selected |
| faster-whisper | 127.0.0.1:8003 | Speech-to-text | Voice input fails |
| oAIo VRAM | 127.0.0.1:9000 | VRAM bar display | Bar stays at 0%, silent |
| Open WebUI | 127.0.0.1:3000 | Chat sync | Sync disabled, silent |

### Hardcoded Paths (must change for portability)

| Value | File | Line |
|-------|------|------|
| `/home/oao/avatars/AvatarSample_K.vrm` | renderer.src.js | 11 |
| `/home/oao/avatars/kira-voice-ref.wav` | config.js | 42 |
| `/home/oao/desktop-waifu/conversation-history.json` | llm.js | 8 |
| Open WebUI JWT token | config.js | 47 |

### Known Issues

1. **X11 fullscreen compositor ghosting** — transparent fullscreen Electron window causes artifacts on KDE/KWin X11. Workaround: 1200px desktop mode.
2. **No retry/timeout on fetch calls** — if any service hangs, app blocks indefinitely
3. **No health checks** — no indication which service is down
4. **JWT token in source** — must strip before pushing to git
5. **Conversation history plaintext** — unencrypted JSON on disk
6. **Race conditions** — concurrent chatStream calls can interleave history, rapid model clicks overlap
7. **Dead code** — setWindowType() defined but never called, disconnect() in lipsync never called, chat() in llm.js never called
8. **Electron security** — contextIsolation=false, nodeIntegration=true, sandbox=false (acceptable for personal local app)
9. **View mode not persisted** — resets to compact on restart
10. **Kokoro+RVC hidden from TTS pills** — backend works but UI only shows Kokoro and IndexTTS

### Not Yet Done

- [ ] Git repo initialization + .gitignore
- [ ] Push to GitHub (zabyxc01/desktop-anime-ai, private)
- [ ] Strip JWT from source → env var
- [ ] ComfyUI model subdirs (checkpoints, loras, vae)
- [ ] Model staging drop zone
- [ ] Fetch timeouts
- [ ] Service health indicator in UI
- [ ] Persist view mode preference
