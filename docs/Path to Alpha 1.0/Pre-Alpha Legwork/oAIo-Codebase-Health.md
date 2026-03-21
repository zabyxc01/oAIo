# oAIo Codebase Health Scan

> Pre-Alpha legwork. 2026-03-21. 3 bots scanned backend/, extensions/, config/docker/frontend/.

---

## BUG FOUND

**`services.py:530`** — Scan age detection reads `scanned_at` but the payload writes `scan_time`. Key mismatch = scan age never populates. Real bug, needs fix.

---

## Backend (16 issues)

### Stale Service References
| File | Line | Service | Issue |
|------|------|---------|-------|
| services.py | 437 | styletts2 | Dead entry in `_DEFAULT_SERVICE_PORTS` |
| services.py | 435 | f5-tts | Dead entry in `_DEFAULT_SERVICE_PORTS` |
| discovery.py | 84-102 | indextts, styletts2, f5-tts | Stale entries in `KNOWN_SERVICE_PLUGINS` |
| discovery.py | 319-322 | indextts | Service directory discovery for removed service |

### Dead Code
| File | Line | What |
|------|------|------|
| services.py | 428-438 | `_DEFAULT_SERVICE_PORTS` dict — never called by autowire |
| vram_realtime.py | 65 | Dead example `"indextts": 6.82` in docstring |
| graph.py | 89 | Dead example `"indextts:tts:text_in"` in docstring |

### Duplicate Logic
| File | What | Duplicate Of |
|------|------|-------------|
| shared.py:204-218 | `build_service_urls()` hardcoded | discovery.py config loader |
| services.py:100-126 | `_derive_io()` port mapping | `_CAPABILITY_PORTS` in same file |

### Bad Connection
| File | Line | Problem |
|------|------|---------|
| services.py | 530-531 | `scanned_at` vs `scan_time` key mismatch (THE BUG) |
| discovery.py | 324-330 | Hardcoded `if service_name in ("f5-tts", "styletts2")` |

### Clean
- No circular imports
- No broken imports
- All 79 API endpoints routed and valid
- Clean core/ → api/ import hierarchy

---

## Extensions (12 issues, all in companion)

### Stale TTS Branches
| File | Lines | Branch | Status |
|------|-------|--------|--------|
| backend.py | 759-771, 804, 859-883 | `engine == "indextts"` | KEEP (show-off tier) |
| backend.py | 783-794, 808, 908-924 | `engine == "f5"` | STALE — delete |
| backend.py | 773-781, 806, 886-905 | `engine == "oaudio"` (RVC chain) | STALE — delete |

### Orphaned Functions
| File | Lines | Function | Callers |
|------|-------|----------|---------|
| tools.py | 97-109 | `describe_screen()` | ZERO |
| tools.py | 112-124 | `remember()` | ZERO |
| tools.py | 127-138 | `recall()` | ZERO |

### Duplicate
| What | Where |
|------|-------|
| `_get_service_url()` | backend.py:242-257 |
| `_get_url()` | tools.py:20-35 (identical logic) |

### Broken Config
| Key | Issue |
|-----|-------|
| `rvc_model` | Read in 3 places, NOT in PATCH whitelist |

### Clean Extensions
- Fleet: clean (736 LoC)
- M3: clean (687 LoC, placeholders intentional)
- Debugger: clean (137 LoC)
- Example: clean (21 LoC)

---

## Config / Docker / Frontend — ALL CLEAN

- All 14 services in services.json match docker-compose.yml
- All modes reference valid services
- All 23 symlinks target valid containers
- All 31 frontend fetch() calls resolve to real backend endpoints
- No orphaned CSS, no broken volumes, no depends_on errors
- nodes.json has stale graph references to deleted services (harmless, graph data)

---

## oprojecto Cross-System Scan (1 bot)

### Stale References Found
| Issue | File | Line |
|-------|------|------|
| TTS dropdown lists "f5" | toolbar.gd | 182 |
| TTS dropdown lists "oaudio" | toolbar.gd | — |
| Default model `gemma3:latest` | pipeline.gd | 22 |
| Vision model hardcoded `llama3.2-vision:11b` | hub_client.gd | 85 |

### Clean
- All WebSocket message types valid both directions
- No MoMask/Florence-2/Letta references
- Animation system clean (87 VRMA working)
- Emotion detection duplicate is intentional (direct mode fallback)
- Direct mode fallback targets valid services (ollama:11434, kokoro:8000, whisper:8003)
