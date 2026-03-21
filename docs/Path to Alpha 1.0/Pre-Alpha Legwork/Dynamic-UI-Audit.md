# Dynamic UI / Service Frontend Audit

> Pre-Alpha legwork. 2026-03-21. 3 bots (existing solutions research, oAIo infrastructure audit, critic).

---

## Verdict: Manifest + Templates, not full auto-discovery.

---

## What oAIo Already Has (80% built)

| Layer | Status | What Exists |
|-------|--------|-------------|
| Service scanning | **Done** | `_scan_service()` — OpenAPI, Gradio v4/v6, OpenAI-compat detection |
| Capability detection | **Done** | `_derive_capabilities()` + `KNOWN_SERVICE_PLUGINS` + `CAPABILITY_PLUGINS` |
| Port/IO generation | **Done** | `_generate_ports_from_scan()` + autowire API + type coercion |
| Graph model | **Done** | 9 data types, compatibility matrix, validation, CRUD, persistence |
| Service registry | **Done** | services.json with capabilities field (structured but unused by frontend) |
| Scan persistence | **Done** | scans.json, service_ports.json cached |
| Discovery routes | **Done** | `/graph/discover`, `/services/{name}/scan`, `/services/{name}/autowire` |
| Frontend rendering | **Missing** | All HTML hardcoded in app.js. No schema-driven forms. |
| Service manifests | **Missing** | No per-service UI declaration |
| UI templates | **Missing** | No pre-built components (audio player, chat, image gen) |

## Existing Solutions Researched

| Tool | What It Does | Gap |
|------|-------------|-----|
| Swagger/RapiDoc/Scalar | Interactive forms from OpenAPI | Single-spec only, no multi-service aggregation |
| Appsmith/ToolJet/Budibase | Admin panels from REST APIs | Manual drag-and-drop, no auto-generation |
| Traefik | Docker label discovery + routing | Knows WHERE services are, not WHAT they do |
| react-jsonschema-form (RJSF) | Render forms from JSON Schema | **Key building block** — could render forms from OpenAPI request bodies |
| Gradio | Auto-generates UI from Python functions | Single-function, no cross-service |
| ComfyUI/Node-RED | Plugin registers capabilities, platform renders UI | Discovery within one process, not across Docker |

**No existing tool combines all three: Docker discovery + API introspection + unified adaptive UI.**

## Bot C Critique — Why Full Auto-Discovery Is Wrong

1. **API discovery is unreliable** — FastAPI exposes clean OpenAPI. Gradio exposes non-standard schema. Flask exposes nothing. Ollama has no self-serve spec. Half the stack needs adapters anyway.

2. **Auto-generated UIs are ugly** — A TTS page needs audio player + waveform + voice selector. Auto-generation gives text inputs + submit button + raw JSON.

3. **The semantic gap kills it** — OpenAPI says `POST /synthesize` takes string, returns bytes. Doesn't say "this is audio, render a player." Every semantic decision needs either service annotation or hardcoded knowledge.

4. **Pipeline composition needs a type system** — Chaining TTS → voice conversion requires knowing sample rates, formats. OpenAPI doesn't express this.

5. **Single user, stable stack** — Auto-discovery solves "unknown services appear." You know your services. A hand-crafted dashboard ships faster and looks better.

## The 80/20: Manifest + Templates

```
Service starts → oAIo scans it (already built)
Scan produces capabilities (already built)
Service has manifest.yaml declaring:
  - category (voice, vision, llm, search)
  - UI template (audio-player, chat, image-gen)
  - field overrides (labels, ranges, dropdowns)
Frontend reads manifest + scan data → renders template
```

### What this gives you:
- One unified frontend (the real value)
- Per-service UIs that look good (template-based, not auto-generated)
- Easy to add services (write a manifest + pick a template, 30 min)
- No schema parsing edge cases

### What this costs:
- 30 min per new service (write manifest)
- 5-6 pre-built templates to create
- Frontend renderer that reads manifests

### Post-Alpha work:
- Define manifest format
- Build 5-6 UI template components
- Build frontend renderer
- Pipeline presets (predefined chains, not visual programming)
