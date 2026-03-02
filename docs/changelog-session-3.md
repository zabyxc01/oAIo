# oAIo — Session 3 Changelog
> Milestone checkpoint — March 2026

---

## What Was Completed

### WebSocket — Replaced 3s Polling
- `backend/ollmo/main.py`: Added `@app.websocket("/ws")` — pushes `vram/gpu/ram/services/alerts` at 1Hz
- `frontend/src/app.js`:
  - Added `applyStatusUpdate(d)` — extracted render logic from poll()
  - Added `connectStatusWS()` — persistent WS with 3s auto-reconnect on close
  - Added `pollStorage()` — storage stats only, polled every 30s (separate from WS)
  - Init block changed: `setInterval(poll, 3000); poll()` → `connectStatusWS(); setInterval(pollStorage, 30000); pollStorage()`
  - `poll()` kept as manual refresh (called after mode activate / template load)
- `backend/requirements.txt`: Added `websockets` (required by uvicorn for WS support)

### Data-Driven Service Registration
- `config/services.json`: Added `capabilities` array to all 6 services
  - `ollama`: models endpoint → `/services/ollama/models`
  - `rvc`: voice models → `/services/rvc/models`
  - `comfyui`: workflows → `/services/comfyui/workflows`
  - `kokoro-tts`: tts voices → `/services/kokoro-tts/voices`
  - `open-webui`, `f5-tts`: empty capabilities (no sub-nodes yet)
- `backend/ollmo/main.py`: Added `SERVICES_CFG_FILE`, `GET /config/services`, `POST /config/services`
- `frontend/src/nodes/services.js`: Replaced hardcoded `SERVICE_DEFS` array with `registerServiceNodes()` — fetches `/config/services`, dynamically registers LiteGraph node types
  - Node labels now include service description from JSON
  - New services can be added via POST `/config/services` without rebuilding

### .env File + docker-compose.yml Variables
- Created `.env` — GPU arch, symlink root, port reference (not committed — in .gitignore)
- Created `.env.example` — documented template committed to repo
- `docker-compose.yml` GPU env vars now use `${VAR:-default}` fallback:
  - `HSA_OVERRIDE_GFX_VERSION=${HSA_OVERRIDE_GFX_VERSION:-11.0.0}`
  - `PYTORCH_ROCM_ARCH=${PYTORCH_ROCM_ARCH:-gfx1100}`
  - `OAIO_SYMLINK_ROOT=${OAIO_SYMLINK_ROOT:-/mnt/oaio}`

### Install Script
- Created `install.sh` — interactive two-step installer
  - Step 1: Deployment type (workstation / training / fleet / custom)
  - Step 2: Component selection with toggle menu (LLM / Voice / Render / Training / Fleet)
  - Step 3: GPU detection (AMD via rocminfo + sysfs, NVIDIA via nvidia-smi, fallback)
  - Writes `.env` from detected GPU + confirmed paths
  - Step 4: Path configuration (interactive, defaults accepted with Enter)
  - Creates 16 symlinks under `/mnt/oaio` (idempotent)
  - Step 5: Builds local images + pulls remote, starts selected services
  - Optional systemd service install
  - Safe to re-run (all operations idempotent)

### Stack Health (verified)
| Container   | Status  | Notes                        |
|-------------|---------|------------------------------|
| oaio        | healthy | ports 9000, 8002 — WS live  |
| ollama      | running | 5 models on SATA             |
| open-webui  | healthy | TTS → rvc:8001/v1            |
| kokoro-tts  | running | 6 voices                     |
| rvc         | running | GOTHMOMMY + Bubble loaded    |
| f5-tts      | running | /clone tested ✅             |
| comfyui     | running | 19 workflows visible         |

VRAM at commit: ~5.7GB / 21.46GB (26%)

---

## Architecture Notes

### WebSocket Push vs Polling
Prior: `poll()` every 3s fetched system/status + storage/stats together — 2 HTTP requests per cycle.
Now:
- `/ws` pushes vram/gpu/ram/services/alerts at 1Hz via WebSocket — always live
- Storage stats polled separately every 30s (slow, doesn't need 1Hz)
- `poll()` retained as manual one-shot refresh (mode activate, template load)
- WS auto-reconnects after 3s on close/error

### Data-Driven Nodes
Service definitions moved from hardcoded JS array to `services.json`. New services:
1. Add entry to `config/services.json` (or POST to `/config/services`)
2. Restart oaio container
3. Node appears automatically in the graph

Capabilities schema:
```json
{ "type": "models|voices|workflows", "label": "...",
  "endpoint": "/services/{name}/models",
  "node_type": "oAIo/...",
  "action_endpoint": "/services/{name}/models/{id}/action",
  "action_method": "POST" }
```

### WebSocket Dependencies
uvicorn needs a WS backend. Added `websockets` to requirements.txt (used by uvicorn internally for WS frame handling). `uvicorn[standard]` also pulls it in — we pin both for clarity.

---

## Remaining for v0.1.0-alpha
| Item | Status |
|------|--------|
| WebSocket (replace 3s polling) | ✅ Done |
| Data-driven service registration | ✅ Done |
| .env file | ✅ Done |
| Install script (two-step flow) | ✅ Done |
| Extension system | → v0.2.0 |
| Git tag v0.1.0-alpha | ← this commit |
