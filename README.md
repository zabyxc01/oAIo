# oAIo

Local AI workstation orchestration platform for SYS-PANDORA-OAO and compatible systems.

oAIo manages GPU memory, service lifecycles, storage tiers, voice pipelines, image generation, and multi-node fleet coordination — all from a single control plane with a 3-tab fluid-grid UI.

---

## Stack

| Container | Image | Port(s) | Role |
|-----------|-------|---------|------|
| oaio | local build | 9000, 8002 | Control plane API + UI |
| ollama | ollama/ollama:rocm | 11434 | LLM inference (ROCm) |
| open-webui | ghcr.io/open-webui/open-webui:main | 3000 | Chat UI |
| kokoro-tts | local build | 8000 | TTS synthesis |
| rvc | local build | 7865, 8001 | Voice conversion proxy |
| f5-tts | local build | 7860 | Voice cloning |
| comfyui | local build | 8188 | Image generation |

---

## Quick Start

```bash
# Clone
git clone https://github.com/zabyxc01/oAIo
cd oAIo

# Configure
cp .env.example .env
# Edit .env — set HSA_OVERRIDE_GFX_VERSION, PYTORCH_ROCM_ARCH for your GPU

# Run install script (sets up symlinks, directories, systemd service)
chmod +x install.sh && ./install.sh

# Start stack
docker compose up -d

# Open UI
http://localhost:9000
```

---

## Architecture

### Symlink Bus (`/mnt/oaio/`)

All service data paths route through `/mnt/oaio/` symlinks. This provides an atomic tier-switching layer — swapping a symlink target instantly redirects any service's data path with zero downtime.

```
/mnt/oaio/models    → /mnt/storage/ai/comfyui/models   (NVMe, default)
/mnt/oaio/models    → /dev/shm/oaio-models              (RAM tier, on demand)
/mnt/oaio/ollama    → /mnt/windows-sata/ollama-models   (SATA)
```

Managed via `POST /config/paths/{name}` — accepts absolute path or `"ram"` / `"default"` shortcuts.

### Storage Tiers

| Tier | Path prefix | Use |
|------|------------|-----|
| NVMe | /mnt/storage | Active models, fast access |
| SATA | /mnt/windows-sata | Ollama model library |
| RAM | /dev/shm/oaio-* | Pinned host memory, fastest load |
| Thunderbolt | configurable | External fast storage |

RAM tier ceiling is auto-detected per machine: `total_ram - max(8GB, 25%)`. Activates via symlink flip, tracked in the WS stream.

### UI — 3-Tab Fluid Grid

**LIVE tab** — cards that update from the 1Hz WS stream:
- **Mode Select** — all modes with VRAM estimate; active modes highlighted
- **VRAM / RAM Accounting** — used / external / headroom with stacked bar
- **Kill Log** — last 10 enforcer events (kill / crash / restore) with timestamps
- **Services** — per-service status dot, start/stop buttons, VRAM estimate, and auto-restore toggle
- **RAM Tier** — path list with current tier badge and NVMe↔RAM toggle
- **Timeline** — resizable heat-map canvas (VRAM + RAM + GPU% + NVMe R/W + SATA R/W); drag handle to resize; view toggles

**CONFIG tab** — LiteGraph node-graph (lazy-loaded; placeholder until clicked)

**ADVANCED tab** — storage paths, routing URLs, templates, API reference

### Resource Enforcement

- **System-aware accounting** — tracks `vram_external` (gaming/other processes) and `vram_headroom` (actually free) via sysfs
- Mode pre-flight checks against real headroom — if a game uses 8GB, mode activation sees only 12GB available and blocks accordingly
- Reactive enforcement loop (5s poll) — kills lowest-priority hard-limit service when VRAM exceeds 95%
- **Kill log** — last 50 events (kill/restore/crash) with VRAM snapshot and timestamp; in WS stream and `/enforcement/status`
- **Recovery** — enforcer-killed containers automatically restarted after 30s when VRAM drops below 85%
- **Per-service `auto_restore`** — toggle per service in the UI or via `PATCH /config/services/{name}`; off = stays stopped after kill
- **Manual stop awareness** — manually stopping a service via UI suppresses crash detection; enforcer won't fight the user
- **Crash detection** — unexpected container exits (OOM, f5-tts startup pressure, etc.) detected and restored with same 30s backoff
- **active_modes persisted** — survives oaio restarts via `config/active_modes.json`; enforcer resumes correct state on boot
- Mode-aware — enforcer pauses when no mode is active (safe for gaming/other GPU use)
- Priority: ollama=1 (protected), rvc/kokoro=2 (soft), comfyui=3, open-webui=4, f5-tts=5 (first to die)
- VRAM total read from sysfs dynamically — not hardcoded
- Stop latency: background thread + 3s SIGKILL timeout — UI responds instantly, container gone within 3s

### Modes

Modes define which services run together and their VRAM budget:

```
CONVERSE  → ollama + open-webui + kokoro-tts + rvc  (11GB budget)
CREATE    → + comfyui                                 (20GB budget)
```

`POST /modes/{name}/activate` — starts all mode services, registers with enforcer
`POST /modes/{name}/check` — dry-run VRAM projection without activating

### Voice Pipeline

```
OpenWebUI → RVC proxy (8001) → Kokoro TTS (8000) → voice output
```

- 6 OpenAI-compatible voice names (alloy, nova, shimmer, echo, fable, onyx)
- 4 distinct Kokoro voices: af_heart, af_sky, bf_emma, am_adam
- RVC voice conversion layer applies character voice on top of Kokoro synthesis
- Model switching: `POST /services/rvc/models/{name}/activate` — uses Gradio API, no restart

Voice cloning (`POST /clone` on port 8002):
```
ref_audio + [ref_text] + target_text → F5-TTS → WAV
```
If `ref_text` is omitted, auto-transcribed via Whisper tiny.

### Extension System

Extensions live in `extensions/<name>/` and are loaded at startup. No core code changes needed to add an extension.

**Manifest format** (`manifest.json`):
```json
{
  "name": "myext",
  "version": "0.1.0",
  "enabled": true,
  "backend": { "router": "backend.py", "prefix": "/extensions/myext" },
  "frontend": { "nodes": ["nodes.js"] }
}
```

Extensions are volume-mounted (`./extensions:/app/extensions`) — file changes take effect on oaio restart only.

**Bundled extensions:**

| Extension | Description |
|-----------|-------------|
| fleet | Multi-node orchestration — register remote oAIo instances, dispatch jobs |
| debugger | Live container log streaming, error filtering, LiteGraph nodes |

---

## API Reference

All endpoints on port 9000 unless noted.

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | /system/status | Full system snapshot |
| GET | /vram | GPU VRAM usage |
| GET | /enforcement/status | Enforcer state, kill order, full kill log |
| WS | /ws | 1Hz push: vram/gpu/ram/ram_tier/accounting/active_modes/services/alerts/kill_log |

### Services
| Method | Path | Description |
|--------|------|-------------|
| GET | /services | List all services |
| POST | /services/{name}/start | Start container |
| POST | /services/{name}/stop | Stop container |
| GET | /services/{name}/status | Container status |
| GET | /services/{name}/logs | Container logs |
| GET | /services/ollama/models | List Ollama models |
| POST | /services/ollama/models/{name}/load | Load Ollama model |
| GET | /services/rvc/models | List RVC models |
| POST | /services/rvc/models/{name}/activate | Switch RVC voice model |

### Modes
| Method | Path | Description |
|--------|------|-------------|
| GET | /modes | List all modes |
| POST | /modes/{name}/activate | Activate mode (start services) |
| POST | /modes/{name}/deactivate | Deactivate mode |
| GET | /modes/{name}/check | VRAM projection (dry run) |
| POST | /modes/{name}/reset | Restore allocations + budget to startup defaults |

### Config
| Method | Path | Description |
|--------|------|-------------|
| GET | /config/paths | All symlink paths + tier + exists |
| POST | /config/paths/{name} | Repoint symlink (or `"ram"`/`"default"`) |
| GET | /config/services | Service registry |
| POST | /config/services | Register new service |
| PATCH | /config/services/{name} | Update service fields (priority, limit_mode, auto_restore, …) |
| GET | /config/routing | Routing config |
| GET | /config/storage/stats | NVMe/SATA MB/s |

### Templates
| Method | Path | Description |
|--------|------|-------------|
| GET | /templates | List saved graph templates |
| POST | /templates/{name}/load | Load template |

### Extensions
| Method | Path | Description |
|--------|------|-------------|
| GET | /extensions | Extension registry |
| POST | /extensions/{name}/enable | Enable extension |
| POST | /extensions/{name}/disable | Disable extension |

### Fleet (extension)
| Method | Path | Description |
|--------|------|-------------|
| POST | /extensions/fleet/nodes/register | Register remote oAIo node |
| GET | /extensions/fleet/nodes | List fleet nodes |
| GET | /extensions/fleet/nodes/{id} | Node detail + live status |
| POST | /extensions/fleet/nodes/{id}/ping | Manual ping |
| DELETE | /extensions/fleet/nodes/{id} | Deregister node |
| POST | /extensions/fleet/jobs | Dispatch job to node |
| GET | /extensions/fleet/jobs | List jobs |
| WS | /extensions/fleet/ws | Fleet-wide status stream (1Hz) |

### Debugger (extension)
| Method | Path | Description |
|--------|------|-------------|
| GET | /extensions/debugger/logs/{container} | Tail container logs |
| GET | /extensions/debugger/errors/{container} | Error/warn lines only |
| WS | /extensions/debugger/ws/{container} | Live log stream |

### oAudio (port 8002)
| Method | Path | Description |
|--------|------|-------------|
| POST | /speak | Text → Kokoro → RVC → MP3 |
| POST | /convert | Audio → RVC conversion → MP3 |
| POST | /clone | Voice cloning via F5-TTS → WAV |
| GET | /voices | List available voices |

---

## Hardware Requirements

**Minimum:**
- GPU: AMD RX 6000+ or NVIDIA RTX 3000+ (8GB VRAM)
- RAM: 16GB
- Storage: 50GB NVMe

**Recommended (reference build — SYS-PANDORA-OAO):**
- CPU: AMD Ryzen 9 3900X
- GPU: AMD RX 7900 XT 20GB (ROCm 6.2, gfx1100)
- RAM: 62GB
- Storage: NVMe (OS + models) + SATA (Ollama library)

**Optional perf gains:**
- Enable SAM (Resizable BAR) in BIOS — full GPU BAR exposure, faster model loads from RAM tier
- Thunderbolt 3/4 external storage for model overflow

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| HSA_OVERRIDE_GFX_VERSION | 11.0.0 | ROCm GFX version override |
| PYTORCH_ROCM_ARCH | gfx1100 | PyTorch ROCm arch |
| OAIO_SYMLINK_ROOT | /mnt/oaio | Symlink bus root |
| OLLAMA_URL | http://ollama:11434 | Ollama API URL |
| KOKORO_URL | http://kokoro-tts:8000 | Kokoro TTS URL |
| RVC_PROXY | http://rvc:8001 | RVC proxy URL |
| RVC_GRADIO | http://rvc:7865 | RVC Gradio URL |
| F5_TTS_URL | http://f5-tts:7860 | F5-TTS URL |
| HF_HOME | /hf-cache | HuggingFace cache path |

---

## Git History

| Tag/Commit | Description |
|-----------|-------------|
| 1797d5d | Initial commit — control plane |
| ce36c94 | Enforcement loop, oAudio pipeline, RVC persistence |
| 919f1d9 (v0.1.0-alpha) | WebSocket, data-driven services, .env, install script |
| 49b1352 | Extension system + fleet extension |
| 82833af | RAM tier — environment-aware pinned host memory |
| 306c74b | Fix: oaio :ro visibility mounts |
| a728869 | Fix: RVC activation via Gradio API |
| 0f37cab | Fix: /clone auto-transcription via faster-whisper |
| 0eb4da9 | Debugger extension |
| 743168c | README |
| 8a5378b | Install.sh polish — system requirements check (Step 0) |
| 22ab9e0 | System-aware resource accounting — headroom-based mode pre-flight, vram_external |
| 5f8f990 | Enforcer: kill log, recovery, crash watch, active_modes persistence, stale cache fix |
| 53be4dd | Cleanup: remove stale SERVICES cache, gitignore active_modes.json |
| de3cd43 | 3-tab fluid grid UI; timeline heatmap; auto_restore toggle; stop latency fix; manual stop crash detection |

---

## License

Private — SYS-PANDORA-OAO / zabyxc01
