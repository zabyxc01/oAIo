---
name: oAIo + Desktop Waifu — Full Stack State (ARCHIVED)
description: ARCHIVED 2026-03-21. Historical snapshot. Electron app archived, replaced by oprojecto (Godot). Current state in docs/Path to Alpha 1.0/. Original date 2026-03-16.
type: project
---

# oAIo + Desktop Waifu — Full Stack State (2026-03-16)

## System — SYS-PANDORA-OAO
- CPU: Ryzen 9 3900X (12C/24T) | RAM: 62GB DDR4 | GPU: RX 7900 XT 20GB (gfx1100, RDNA3)
- OS: Ubuntu 24.04 LTS | KDE Plasma 5.27 | X11 (NOT Wayland)
- Kernel: 6.17.0-14-generic | Mesa 25.2.8
- ROCm: Docker only (never on host)
- Docker: json-file log driver, 50MB x 3 rotation
- Monitor: Dell AW3425DW 3440x1440 ultrawide

## oAIo — /mnt/windows-sata/oAIo/

**GitHub:** https://github.com/zabyxc01/oAIo (public)

### Docker Stack (10 containers)
- All services `restart: "no"` except oaio (`restart: unless-stopped`)
- Services start on demand via modes, waifu presets, or individual toggle
- All ports bind to `${OAIO_BIND:-127.0.0.1}`

### Graph Engine (built 2026-03-15/16)
- `core/graph.py` (310 lines) — Node, Plugin, Port, Edge, GraphState data model + type system
- `core/discovery.py` (290+ lines) — auto-discovers 9 services with typed plugins/ports, per-model Ollama discovery, directory discovery for all services
- `core/router.py` (216 lines) — RouteManager with on-demand + auto sync modes
- `core/vram_realtime.py` (75 lines) — per-container VRAM via /proc fdinfo
- 20+ API endpoints under /graph/*, /enforcement/mode
- CONFIG tab: graph-discovered nodes with colored port types, plugins panel, dir listing, save-graph-mode, host selector, + MODE group frames, + NODE discovery + external tools

### Recent oAIo Commits
```
fd9c236 fix: services default to stopped
60ce12d chore: persist runtime state
b203373 feat: + MODE group frames, + NODE discovery + external tools
62fbee4 feat: CONFIG tab plugins, port colors, dir discovery, save-graph-mode, host
410cfec feat: graph-engine frontend + mode-graph binding + router
1cc4faa feat: graph engine core
3073d1d feat: real-time per-container VRAM tracking + enforcement toggle
```

---

## Desktop Waifu — /home/oao/desktop-waifu/

**GitHub:** https://github.com/zabyxc01/Desktop-Anime-AI (private)

### Current Layout: "Taskbar" View
- Full monitor width (3440px), half height (~720px), anchored to bottom
- Avatar column flex-grows left, chat panel 420px fixed right
- Bottom controls strip: [Modes | Waifu | Services] tabs + VRAM bar + gear + input
- All transparent — desktop wallpaper visible through
- `_NET_WM_STATE_BELOW` keeps window behind normal windows
- `setIgnoreMouseEvents(false)` — UI always interactive (X11 forward:true unreliable)

### Avatar Behavior: Awareness State Machine
- **SITTING_AWAY** (default): sitting on taskbar edge facing away, torso upright, 90° hip bend, 90° knee, feet hanging
- **NOTICING**: mouse detected → head turns over shoulder (0.6s)
- **GETTING_UP**: pushes up, turns around to face camera (1.5s smoothstep)
- **STANDING**: normal idle — breathing, mouse head tracking, weight shifting, blink
- **SITTING_DOWN**: 30s no mouse → sits back down and turns away (1.2s)
- Spring bones active if model has them (hair/accessories physics)
- Mouse head tracking: head/neck follow cursor position, biased for right-side avatar position

### UI: Mode/Service/Waifu Tabs
**Modes tab**: oAIo mode pills (oLLMo, oAudio, Waifu, etc.) — click to activate/deactivate
**Waifu tab**: 5 purpose-built presets:
  - Text + Kokoro (2 containers)
  - Text + IndexTTS (2 containers)
  - Voice + Kokoro (3 containers)
  - Voice + IndexTTS (3 containers)
  - Voice + RVC (4 containers)
  Each starts only required containers, sets correct TTS backend, stops previous preset's extras
**Services tab**: individual service toggles — one at a time unless in a mode

### Voice Pipeline
- STT: faster-whisper on CPU (F2 push-to-talk)
- LLM: Ollama (gemma3 default), streaming with 200-char sentence split threshold
- TTS: Kokoro (fast), Kokoro+RVC (quality), IndexTTS (voice clone) — selectable in settings
- All fetch calls have timeouts (TTS 30s, STT 15s, LLM 60s, sync 10s)
- Service health dots: pulsing red indicator when service is down

### Settings (gear icon, expands upward from input bar)
- View mode (compact/desktop)
- TTS backend
- Kokoro voice
- LLM model
- Clear history / Quit

### Hotkeys
F2=PTT, F3=walk, F4=jumping jacks, F5=settings, Escape=quit

### Recent Waifu Commits
```
f63a0fa feat: Waifu presets tab
aa72338 cleanup: remove redundant pills from bottom bar
113b086 feat: Mode/Service toggle panel
fdecb49 fix: click-through toggle
d1cf82c feat: taskbar view
c89cc8f feat: model pills right-aligned, avatar slides
727a4c6 fix: compact layout full width half height
6144d21 feat: fetch timeouts, health, persistence, cleanup
aa4614f feat: initial commit
```

### Known X11 Limitations
- Fullscreen transparent window causes KWin compositor ghosting — using half-height workaround
- `forward: true` pixel alpha check unreliable on X11 — using `setIgnoreMouseEvents(false)` instead
- `_NET_WM_STATE_BELOW` may deprioritize input — bypassed with always-interactive window
- True fullscreen needs Wayland/Plasma 6

### Hardcoded Paths (portability blockers)
- `/home/oao/avatars/AvatarSample_K.vrm`
- `/home/oao/avatars/kira-voice-ref.wav`
- `/home/oao/desktop-waifu/conversation-history.json`
- JWT token in `.env` (stripped from source)

---

## What's Done
- [x] Graph engine (nodes, typed ports, edges, discovery, router)
- [x] CONFIG tab graph integration (port colors, plugins panel, save-graph-mode)
- [x] Real-time per-container VRAM tracking + enforcement mode toggle
- [x] Services default to stopped (restart: "no")
- [x] Waifu presets (5 service combos)
- [x] Mode/Service/Waifu tabs in bottom bar
- [x] Awareness state machine (sitting → noticing → standing → sitting)
- [x] Mouse head tracking + weight shifting + spring bones
- [x] Fetch timeouts + service health indicators
- [x] Docker log rotation
- [x] All ComfyUI model subdirs created
- [x] Both repos pushed clean to GitHub

## What's Left
- [ ] Restore GPG keys from backup
- [ ] EVE tools stack
- [ ] Router in production (plumbing done, waifu still talks to services directly)
- [ ] CONFIG bottom detail panel — per-node plugin config with dot management
- [ ] Save wiring as mode from CONFIG UI (button exists, needs group-node detection polish)
- [ ] VMD/VRMA animation files for richer motion
- [ ] Sitting pose fine-tuning (angles, Y position relative to taskbar)
