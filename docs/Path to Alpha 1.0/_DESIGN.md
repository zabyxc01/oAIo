# Design Principles — oAIo + oprojecto

> Established 2026-03-21 during pre-Alpha recentering session. These principles govern all future development.

---

## 1. oAIo is the Brain, Everything Else is a Body

oAIo is the service management and AI compute layer. It runs in Docker, manages resources, enforces VRAM budgets, routes requests, and hosts the companion persona.

Companion clients (oprojecto, Android, web) are bodies — thin clients that render the avatar, capture input, and play audio. They connect via WebSocket and don't care what services are running behind oAIo.

## 2. The Companion WebSocket Protocol is the Client SDK

Any frontend that implements 8 message types is a Kira client:
- Client sends: `chat.request`, `stt.audio`, `vision.analyze`, `state.sync`, `ping`
- Server sends: `chat.response`, `tts.audio`, `stt.transcript`, `state.sync`, `config.sync`, `pong`

Pure JSON over WebSocket. No Godot-specific, no Kotlin-specific, no browser-specific encoding. The protocol is the contract.

## 3. Every Service Exposes a Clean HTTP API

Gradio UIs are debug tools, not user-facing frontends. If a service only has Gradio, wrap it in a FastAPI proxy (the rvc_proxy.py pattern).

oAIo is the only frontend for service management. Users interact with services through oAIo's hub UI, not through 7 separate Gradio tabs.

## 4. Manifest + Templates, Not Full Auto-Discovery

oAIo already has 80% of service discovery infrastructure built (scanning, capability detection, port generation, graph model). Full auto-discovery is overkill for a single-user system.

Instead: services declare their UI category via a manifest. The frontend renders pre-built templates (audio-player, chat, image-gen, text, config) bound to scan data. Adding a service = write a manifest + pick a template (30 minutes).

## 5. Services Stay in Compose

`restart: "no"` means services only start when a mode/profile activates them. Don't remove services from docker-compose.yml — they're available for different modes (gaming, training, show-off, full-stack).

Clean stale code paths. Don't remove infrastructure.

## 6. Pipeline Presets, Not Visual Programming

Predefined tested chains in config (e.g., "text → kokoro → output" or "text → indextts → rvc → output"). Not arbitrary node graphs that users compose visually. The visual programming legwork is someone else's job — document the infrastructure, don't build the editor.

## 7. The LLM Decides, Not Regex

Regex handles pre-filtering (explicit user intent like "remember that X"). The LLM handles response quality, tool selection, and nuance. Don't bypass the LLM with regex workarounds — teach it.

## 8. One Source of Truth Per Concern

- Profiles subsume modes + presets + enforcer settings
- `config/defaults.json` holds all defaults (not scattered .get() calls)
- The enforcer controls resources, everything else reads from it
- The symlink bus routes all data paths

## 9. Frontend-Agnostic Backend

Every backend system exposes structured JSON over REST and streaming events over WebSocket. Zero HTML, zero rendering opinions. Any frontend plugs in identically — hub dashboard, mobile app, CLI tool, Grafana panel.

## 10. No Workarounds Without Documentation

If the plan says X and implementation does Y, the deviation is documented in a change doc per `_RULES.md`. No silent workarounds. No "chimp look it work" — either it's the right solution or it's an explicit workaround with tradeoffs documented.
