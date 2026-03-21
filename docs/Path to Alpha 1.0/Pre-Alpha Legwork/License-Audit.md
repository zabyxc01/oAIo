# License & Dependency Audit

> Pre-Alpha legwork. 2026-03-21. 90+ dependencies catalogued.

---

## License Types in Use

| License | What It Means | Your Deps |
|---------|--------------|-----------|
| **MIT** | Do anything, keep copyright notice | LiteGraph (removed), beautifulsoup4, StyleTTS2, RVC, Letta, Florence-2, IndexTTS, OkHttp, faster-whisper, faiss, curl |
| **Apache 2.0** | MIT + patent protection | Ollama, F5-TTS, MoMask, FastAPI, transformers, diffusers, Kotlin, Gradio, httpx |
| **BSD** | Same as MIT | Uvicorn, psutil, soundfile, numpy, scipy, scikit-learn |
| **LGPL-2.1+** | Use freely, share modifications to THE LIBRARY only | ffmpeg, libsndfile |
| **GPL-3.0** | Copyleft — but container boundary protects you | ComfyUI, espeak-ng |
| **AGPL-3.0** | Network copyleft — unmodified Docker = fine | Open WebUI, SearXNG |

## Key Findings

- **Container architecture is the license firewall.** Every copyleft dep (GPL, AGPL) runs in its own container, communicating over HTTP. Your code never links to theirs.
- **AGPL-3.0 (Open WebUI, SearXNG)**: Running unmodified stock Docker images = no obligation. If you fork and modify the source, must publish changes.
- **GPL-3.0 (ComfyUI, espeak-ng)**: Running as separate processes in containers = not a derivative work.
- **No license issues with current stack.**

## LiteGraph Cleanup (completed this session)
- 36,855 lines removed (litegraph.js, litegraph.css, 5 extension nodes.js files)
- Was MIT licensed, dead code, never loaded after frontend redesign
- Backend references remain (data structures, skip prefixes) — safe, no runtime impact
