# oprojecto Stack Audit — Engine, Architecture, Subsystems

> Pre-Alpha legwork. 2026-03-21. 3 bots (alternatives research, true cost from code, critic).

---

## Verdict: Stay on Godot 4.6. Stay 3D VRM. Architecture is correct.

---

## Engine Alternatives — All Have Dealbreakers on Linux

| Engine | Dealbreaker |
|--------|------------|
| **Unity + UniVRM** | Transparent overlay on Linux NOT SUPPORTED (Windows-only Win32 API) |
| **Electron + three-vrm** | Disabling HW accel for click-through KILLS WebGL 3D performance. Already tried, archived. |
| **Tauri + three-vrm** | Click-through on Linux NOT IMPLEMENTED (open feature request #13070) |
| **Qt/QML** | ZERO VRM ecosystem — build loader/SpringBone/MToon from scratch |
| **Bevy (Rust)** | Pre-1.0 (v0.18), experimental VRM, overlay undocumented |
| **Unreal** | Overkill, no VRM, no overlay |

## VRM Ecosystem — godot-vrm is correct choice

- godot-vrm: 414 stars, MIT, actively maintained, Godot 4 native
- UniVRM: most mature but Unity-locked
- three-vrm: JS reference impl, for web body (future)
- Live2D: proprietary, 2D-only, editor doesn't run on Linux. Would lose 7 models + 87 animations.
- Inochi2D: open source, 2D-only. Same limitation.

---

## Subsystem Audit (from code)

### Project Stats
- 25 .gd files, ~6,700 LoC main + ~6,300 LoC addons = ~13K total
- 7 subsystems: avatar, network, voice, awareness, UI, behavior, engagement

### Subsystem Coupling

| Subsystem | LoC | Godot Coupling | Portability | Porting Effort |
|-----------|-----|----------------|-------------|----------------|
| VRM System | 1,436 + 6,300 addons | 80% | 40% | 3 months |
| Network | 311 | 60% | 70% | 1-2 weeks |
| Voice Pipeline | 576 | 70% | 75% | 2-3 weeks |
| Screen Awareness | 500+ | 75% | 35% | 4-6 weeks |
| UI | 600+ | 85% | 60% | 1 week |
| Behavior | 200 | 40% | 95% | 2-3 days |

### Key Lock-Ins (why Godot is RIGHT)
1. `per_pixel_transparency` + `window_set_mouse_passthrough()` — no other engine on Linux
2. Skeleton3D bone manipulation — VRMA pre-converted for Godot API
3. AudioServer bus architecture — tight mic/TTS/peak monitoring integration
4. Signal-driven architecture — pervasive, clean state change propagation

### Platform-Locked (Linux/X11 specific)
- Screen capture: ffmpeg x11grab (Linux only)
- Screen listen: PipeWire pw-record (Linux only)
- Window queries: xdotool (X11 only)
- Desktop physics: xprop for window geometry (X11 only)

---

## Structural Risks

### 1. main.gd is a 982-line god object (#1 RISK)
- 275-line `_ready()` manually constructs and wires 7 subsystems
- Every future feature makes it worse
- Fix: decompose into .tscn scenes (already on TODO)
- Estimated: 2-3 days dedicated session

### 2. Hardcoded 3440x1440 in screen_capture.gd
- Breaks on any other resolution
- Fix: read from DisplayServer.window_get_size()

### 3. Race condition in screen_capture
- Polls file size instead of checking ffmpeg PID finished
- Can read incomplete screenshots

### 4. Android needs platform abstraction (4-6 weeks)
- xdotool, xprop, ffmpeg x11grab, pw-record don't exist on Android
- Not a rewrite — abstraction layer that swaps Linux tools for Android APIs

---

## Multi-Body Architecture

| Body | Engine | Purpose | Status |
|------|--------|---------|--------|
| Desktop | Godot 4.6 (oprojecto) | Transparent overlay, VRM, screen awareness, physics | Working |
| Android | Native Kotlin | VoiceInteractionService (replaces Google Assistant), touch UI | Scaffolded in /android/ |
| Web | Lightweight JS | Browser-based Kira, companion page in hub | Not started |

All three share the same WebSocket protocol (8 message types, pure JSON):
- Client sends: chat.request, stt.audio, vision.analyze, state.sync, ping
- Server sends: chat.response, tts.audio, stt.transcript, state.sync, config.sync, pong

The companion WebSocket protocol IS the client SDK. Any frontend that implements these 8 message types is a Kira client.

---

## Bot C Criticisms

### Team A oversold:
- Electron (already tried and archived — Team A didn't know project history)
- Tauri (missing feature presented as limitation, not dealbreaker)
- AT-SPI (inconsistent per-app, impractical as primary awareness)

### Team B too conservative:
- Accepted main.gd as normal instead of flagging it as #1 structural risk
- Inflated portability numbers — hub_client.gd is 100% Godot API despite portable JSON protocol
- Missed HTTP/WS protocol mixing in main.gd (5 places construct HTTP URLs alongside WS connection)
