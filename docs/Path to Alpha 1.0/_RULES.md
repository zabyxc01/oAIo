# Rules for This Directory

## The Origin

**Origin Ignition Prompt.md** is the master plan. It does not get edited. It is the baseline that everything is measured against.

## The Rule

Every time a phase or sub-phase is implemented, a **new document** is created in this directory that:

1. **Names the phase** it implements (e.g., `Phase-2A-Health-Checks.md`)
2. **Cites the original section** from the Origin — quote the relevant plan text
3. **Documents what actually changed** — files modified, lines added/removed, commits
4. **Explains WHY** anything deviated from the plan — if the implementation differs from what the Origin describes, explain the reason. No workarounds without explanation.
5. **No workarounds.** If the plan says X and you do Y instead, that is a deviation. Document it. If you can't do X, explain why and get user approval before proceeding with Y.

## Format

```markdown
# Phase {N}{Letter} — {Name}

## Original Plan (from Origin Ignition Prompt)

> (quoted text from the Origin section)

## What Was Implemented

- File: path — what changed
- File: path — what changed

## Deviations from Plan

(If none: "None. Implemented as planned.")

(If any: what changed, why, and whether user approved the deviation.)

## Commits

- `abc1234` — commit message

## Date

YYYY-MM-DD
```

## Build Principles (enforced)

These were established during the pre-Alpha recentering (2026-03-21). Full rationale in `_DESIGN.md`.

1. **Services stay in docker-compose.** Clean stale code paths, don't remove services. `restart: "no"` controls lifecycle.
2. **Gradio UIs are debug-only.** oAIo is the only user-facing service frontend. If a service only has Gradio, wrap it in a FastAPI proxy.
3. **The companion WebSocket protocol is the client SDK.** oprojecto, Android, and web are all clients of the same 8-message protocol.
4. **No workarounds without documentation.** If you can't do X, explain why and get approval before doing Y.

## What Is NOT Allowed

- Editing the Origin Ignition Prompt
- Skipping the change document
- Implementing something different from the plan without documenting why
- Workarounds that bypass the plan silently
- Deleting any document in this directory
- Removing services from docker-compose.yml without explicit user approval
- Building user-facing Gradio frontends (wrap in FastAPI proxy instead)
