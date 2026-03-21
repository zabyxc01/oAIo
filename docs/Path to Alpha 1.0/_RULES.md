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

## What Is NOT Allowed

- Editing the Origin Ignition Prompt
- Skipping the change document
- Implementing something different from the plan without documenting why
- Workarounds that bypass the plan silently
- Deleting any document in this directory
