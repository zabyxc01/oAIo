# oAIo Stack Audit — Service-by-Service Findings

> Pre-Alpha legwork. 2026-03-21. 7 bots (3 alternatives research, 3 true cost from code, 1 critic).

---

## TTS/Voice (5 services audited)

### Kokoro-TTS — DAILY DRIVER
- Hot path: every TTS response via `POST /v1/audio/speech`
- Voice: `af_heart` (companion.json)
- CPU/ONNX, 0.8GB RAM, ~500ms latency
- Batch mode default, sentence-level streaming available
- Called from companion/backend.py:749-757

### RVC — AVAILABLE, NOT IN COMPANION HOT PATH
- Voice conversion proxy: Kokoro output → GOTHMOMMY.pth → Kira's voice
- 3.0GB VRAM idle
- rvc_proxy.py wraps Gradio internals in FastAPI (the pattern for all Gradio services)
- Open WebUI routes through RVC at port 8001
- Drops from oprojecto frontend — Kokoro standalone is the new default

### IndexTTS — SHOW-OFF TIER
- Voice cloning via reference audio (`ref_audio: avatar_voice.wav`)
- 8.0GB VRAM
- Wired in "max_quality" preset and "oaudio-optimized" mode
- Called from backend.py:759-771 when `engine == "indextts"`

### F5-TTS — AVAILABLE, UNTESTED
- /clone endpoint exists but never tested end-to-end
- 2.0GB VRAM
- In "oaudio" mode allocation but not in default companion config
- Code path exists: backend.py:783-794, 908-924

### StyleTTS2 — AVAILABLE, NO HTTP API
- Gradio-only (port 7870), no FastAPI wrapper
- 1.5GB VRAM
- Not in any mode. Zero code references call it.
- If needed: wrap in FastAPI proxy like rvc_proxy.py

### Alternatives Researched
- **Chatterbox Turbo**: MIT, ~2-4GB VRAM, <200ms streaming, zero-shot cloning + emotion, official ROCm support. Could replace all 5 with 1.
- **Qwen3-TTS 0.6B**: Apache 2.0, 97ms first-packet, but only 0.6B params (quality concern)
- **CosyVoice2**: Apache 2.0, ~2-4GB, 150ms streaming, documented AMD
- **Fish Speech 1.5**: ELIMINATED — CC-BY-NC-SA (non-commercial)
- **XTTS v2**: ELIMINATED — Coqui dead Dec 2025
- **Piper/MeloTTS/Parler**: No voice cloning

---

## STT

### faster-whisper — WORKS, MODEL MISMATCH
- CTranslate2 backend, CPU-only
- BUG: Dockerfile says `medium`, compose overrides to `small` — actually running small
- Batch-only (no streaming), 2-5s latency on Ryzen 9
- Called from companion/backend.py:941

### Alternatives Researched
- **Moonshine v2**: MIT, 245M params, 6.65% WER (better than whisper medium ~9-10%), 258ms native streaming, CPU-only
- **whisper.cpp Vulkan**: BROKEN on RDNA3 — models > small crash silently
- **Distil-Whisper large-v3**: Drop-in (change one env var), marginal improvement, no streaming
- **Parakeet/Canary (NeMo)**: Best WER (5.63%) but CUDA-only

---

## Search

### SearXNG — WORKS MECHANICALLY
- RAG waterfall 5th priority, only for factual questions
- 0.3GB RAM, lightweight
- AGPL-3.0 unmodified Docker = no license issue
- Search finds results, page scraping works via beautifulsoup4
- Problem: qwen2.5:7b doesn't use the results correctly (hallucination, not search)

---

## Memory

### Letta — DEAD CODE
- remember() and recall() in tools.py have ZERO callers
- Custom RAG waterfall (694 LoC) handles all memory: knowledge, notes, episodes, vision, web
- 1.0GB RAM if running, never started

### Custom RAG Waterfall — WELL ENGINEERED
- 694 LoC across: rag.py (306), knowledge.py (175), notes.py (79), episodes.py (71), vision_memory.py (63)
- Priority: knowledge → notes → episodes → vision → web → git
- Proper provenance tags, confidence scoring, objective/personal mode split
- Problem is at the MODEL level, not the pipeline

---

## LLM

### Ollama — ESSENTIAL
- All chat + vision (qwen2.5:7b + llava:7b)
- 7.5GB VRAM, ROCm, 77-100+ t/s
- Multi-model hot-swap supported
- Vision path: companion/backend.py:1349-1414 → ollama /api/chat with vision_model

### Alternatives Researched
- **llama.cpp Vulkan**: ~90 t/s on gfx1100, within 10% of ROCm. But loses model management and 92-endpoint integration.
- **vLLM**: gfx1100 exists but Flash Attention disabled, overkill for single-user
- **SGLang**: REJECTS gfx1100 explicitly
- **TGI**: Datacenter GPUs only

---

## Vision

### Florence-2 — DEAD CODE
- describe_screen() in tools.py has zero callers
- Vision pipeline uses LLaVA via Ollama instead
- 2.0GB VRAM wasted if started

### Future: Qwen2.5-VL
- Available in Ollama as qwen2.5vl:7b
- Unifies chat + vision into one model load
- Eliminates need for separate vision model

---

## Motion

### MoMask — DEAD CODE
- Never called by companion or oprojecto
- 87 pre-made VRMA animations used instead
- Build incomplete, 3.0GB VRAM
- All text-to-motion models produce realistic motion, not anime-style

### Future: LLM Animation Selection
- LLM picks from 87 animations via function calling
- Zero VRAM cost, ~100ms latency
- Uses existing infrastructure
