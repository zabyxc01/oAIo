# oAIo — Session 2 Changelog
> Milestone checkpoint — March 2026

---

## What Was Completed

### Server-Side Node Config
- Replaced `localStorage` with server-side persistence
- New file: `config/nodes.json` — stores all node + mode UI configs across sessions
- New endpoints: `GET /config/nodes`, `POST /config/nodes`
- `app.js` — `nodeConfigs` and `modeConfigs` now load from API on init, save to API on change
- Discovered and migrated `oaio-modeConfigs` (was also in localStorage, now server-side)

### oAudio — /convert (audio file → RVC → MP3)
- Root issue: Gradio `infer-web.py` (port 7865) has no pre-loaded model state — wrong door
- Fix: added `POST /convert` endpoint directly to `rvc_proxy.py` (port 8001)
  - Accepts multipart file upload
  - Runs `vc.vc_single()` directly — model already loaded at proxy startup
  - Returns MP3 (matches TTS pipeline output format)
- `rvc_proxy.py` now mounted as a volume (`./docker/rvc/rvc_proxy.py:/rvc/rvc_proxy.py`) — persistent, no rebuild needed to update
- `oaudio/main.py` `/convert` now POSTs file bytes to `http://rvc:8001/convert`

### oAudio — /clone (ref audio + text → F5-TTS → WAV)
- Fixed payload: removed stale `"F5-TTS"` model string, added `randomize_seed` + `seed_input` (F5-TTS API changed to 9 params)
- Fixed file object format: added `"meta": {"_type": "gradio.FileData"}` required by newer Gradio
- **Note:** `ref_text` must be provided — if empty, F5-TTS attempts auto-transcription via Whisper which requires `torchcodec`/FFmpeg libs missing from container

### RVC Voice Models — Persistent Volume
- Created `/mnt/storage/ai/audio/rvc-weights/` and `/mnt/storage/ai/audio/rvc-indices/`
- Both model files now on NVMe (persistent across container rebuilds):
  - `GOTHMOMMY.pth` + `added_GOTHMOMMY_v2.index`
  - `TADC_Bubble.pth` + `added_IVF12_Flat_nprobe_1_TADC_Bubble_v2.index`
- New symlinks: `/mnt/oaio/rvc-weights`, `/mnt/oaio/rvc-indices`
- Compose mounts both into `/rvc/assets/weights` and `/rvc/assets/indices`
- Drop any `.pth` into `/mnt/oaio/rvc-weights/` → appears in RVC automatically

### Symlink Layer — Expanded to 16
- `setup-oaio-symlinks.sh` updated from 8 → 16 symlinks
- Added: `rvc-weights`, `rvc-indices` (this session)
- Script now uses `ln -sfn` (idempotent — safe to re-run)
- `config/paths.json` updated to match (16 entries)

### Stack Health (verified)
| Container   | Status  | Notes                        |
|-------------|---------|------------------------------|
| oaio        | healthy | ports 9000, 8002             |
| ollama      | running | 5 models on SATA             |
| open-webui  | healthy | TTS → rvc:8001/v1            |
| kokoro-tts  | running | 6 voices                     |
| rvc         | running | GOTHMOMMY + Bubble loaded    |
| f5-tts      | running | /clone tested ✅             |
| comfyui     | running | 19 workflows visible         |

VRAM at time of commit: ~8GB / 21.46GB (37%)

---

## Architecture Notes

### Why Gradio Was Bypassed for /convert
RVC runs two separate systems in one container:
- `infer-web.py` (Gradio, port 7865) — model selected via UI state, not pre-loaded
- `rvc_proxy.py` (FastAPI, port 8001) — GOTHMOMMY loaded at startup via `vc.get_vc()`

Calling `infer_convert` via Gradio requires the model to be "selected" through the UI first. The proxy already has it loaded. Added `/convert` to the proxy — cleaner, faster, no Gradio state dependency.

### /clone ref_text Requirement
F5-TTS auto-transcription path (`ref_text=""`) fails because:
`asr_pipe` → `import torchcodec` → missing `libavutil.so.57` in container

Workaround: always pass `ref_text`. When the voice panel is built in the UI, make `ref_text` a required field with a clear label.

### RVC Models Going Forward
All `.pth` files belong in `/mnt/storage/ai/audio/rvc-weights/`.
All `.index` files belong in `/mnt/storage/ai/audio/rvc-indices/`.
Never `docker cp` models — they won't survive a container recreate.

---

## Remaining Alpha Track
| Item | Status |
|------|--------|
| Server-side node config | ✅ Done |
| Audio /convert + /clone | ✅ Done |
| Symlinks script (16 paths) | ✅ Done |
| Stack health verified | ✅ Done |
| Git commit | ← this commit |
| Data-driven service registration | Pending |
| WebSocket (replace 3s polling) | Pending |
| .env file | Pending |
| Install script (two-step flow) | Pending |
| Extension system | Pending |
| Git tag v0.1.0-alpha | Pending |
