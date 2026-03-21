# Wifu Research Agenda — Pipeline Architecture

> Pre-Alpha legwork. 2026-03-21. The master research topic that connects all sub-topics.

---

## The Question

What is the OPTIMAL order for the full companion pipeline? What fires when, what feeds what, and is the current sequence right?

---

## Current Pipeline (as built)

```
INPUT
  chat text / STT transcript / screenshot / ambient audio
    ↓
RAG ENRICHMENT (before LLM)
  knowledge → notes → episodes → vision → web
    ↓
LLM CALL
  system prompt + persona + RAG context + history → response
    ↓
EMOTION DETECTION (after LLM, from response text)
  parse [emotion:intensity] tag OR keyword fallback
    ↓
TTS
  text → Kokoro → audio
    ↓
OUTPUT TO CLIENT
  text + emotion + audio → oprojecto renders face/body/voice
```

## What Needs Research (in order)

### 1. Input Routing
- Chat text, STT transcript, ambient audio, screenshot — how do these compete?
- Should ambient audio + screenshot feed context WITHOUT triggering a response?
- Current: all inputs can trigger LLM call. Should some only update context silently?
- Screen_listen overprocessing problem lives here

### 2. RAG Enrichment
- Current: waterfall runs BEFORE LLM call, injects context
- Alternative: LLM decides what to look up (tool use pattern — LLM says [search: X], backend fetches, feeds back)
- Hybrid: pre-fetch knowledge/notes (cheap, local), let LLM request web/vision (expensive, slow)
- Small model accuracy: qwen2.5:7b ignores RAG context. Bigger model? Better prompting? Two-pass?
- Embedding retrieval vs substring search (nomic-embed-text)

### 3. LLM Call
- Model selection: 7B enough? 14B with freed VRAM? Vision model (qwen2.5-VL) unifying chat+vision?
- Context window management: persona + RAG + history + emotion instructions competing for tokens
- Tool use: LLM outputs [search: query], [save_note: text], [animation: wave] — backend parses and executes
- Second-pass option: first call generates response, second call fact-checks against sources

### 4. Emotion Detection
- Current: detected AFTER LLM response, from response text
- Alternative: emotion detected from USER input first, influences how LLM responds (empathetic response)
- Both? User emotion → influences LLM tone → LLM emotion in response → drives avatar
- Mood drift: rolling window of recent emotions affects baseline (already partially built in persona.py)

### 5. Animation Selection
- Current: emotion maps to blend shapes only. Body animation is random from flat list.
- Optimal: LLM selects animation via function calling [animation: excited_jump]
- OR: emotion→animation mapping table (happy → happy_bounce, sad → sad_slump)
- Requires: 87 VRMA categorized into Idle/Emotion/Action/Reaction layers
- Blend tree: idle always plays, emotion overlays, action interrupts, reaction is one-shot

### 6. TTS
- Current: Kokoro (daily), IndexTTS (show-off). Sequential — waits for full LLM response then speaks.
- Optimal: sentence-level streaming — speak first sentence while generating second
- Emotion→voice: should detected emotion influence TTS parameters (speed, pitch, emphasis)?
- Already partially built: tts_mode: "stream" splits by sentence

### 7. Output Assembly
- Current: text + emotion + audio sent as separate WebSocket messages
- Should they be synchronized? (emotion arrives → face changes → audio arrives → lips move)
- Latency budget: how long from user input to first audio output?
- Streaming chunks: text appears as typed, audio plays as generated, emotion updates as detected

---

## Research Approach

One step at a time. For each sub-topic:
1. Research what's OPTIMAL (state of the art, what other companion/agent systems do)
2. Compare against what we HAVE
3. Decide: keep / modify / rebuild
4. Test the change in isolation before integrating

## Sub-Topic Dependencies

```
Input Routing ──→ RAG Enrichment ──→ LLM Call ──→ Emotion ──→ Animation
                                        ↑              ↓          ↓
                                   Tool Use Loop    TTS Voice   Blend Tree
                                        ↓              ↓          ↓
                                   RAG (2nd pass)  Output Assembly
```

Each builds on the previous. Don't redesign animation if the LLM call order is wrong.

---

## Status

All parked. Research sessions to be scheduled alongside or after Alpha Phase 1-2.
