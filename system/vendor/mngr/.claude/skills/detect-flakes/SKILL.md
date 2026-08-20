---
name: detect-flakes
description: Sweep CI over a time window for every flaking test, then reconcile the whole MIND flake backlog. The scheduled/on-demand batch entry -- it hands the full flake set to manage-flakes and, having seen the full window, is the only path allowed to close stale tickets. Use to detect or triage CI flakes repo-wide; for a single flake you just hit, use report-incidental-flakes.
---

# Detect CI flakes (full-window sweep)

The batch entry point for flake reconciliation. It does two things: sweep CI for every test that flaked in a window, then hand the full set to the `manage-flakes` skill, which owns the clustering, prioritization, and Linear filing -- do not redo any of that here.

Because this path saw the whole window, it -- and only it -- may close stale tickets: a test absent from a full sweep has stopped flaking, whereas absence from a partial set (e.g. one reported via `report-incidental-flakes`) proves nothing.

## Preconditions

- `gh` authenticated for the repo whose CI you are sweeping (the CLI's default unless overridden).
- `manage-flakes`' Linear precondition (latchkey), since you hand off to it.

## Steps

1. **Sweep.** Capture the window's flakes (slow; write to a file, then read the file):

   ```bash
   uv run python scripts/flake_reconcile.py list-flakes > /tmp/flakes.json
   ```

   The CLI's defaults are right for the routine sweep; adjust `--window-days` (or other flags -- see `--help`) only if asked. Read the output: each record is one flaky test with its evidence.

2. **Reconcile.** Apply `manage-flakes` to the full set, stating that it came from a full-window sweep and what the window was -- that is what authorizes closing stale tickets. Everything downstream (clustering, priorities, ticket create/update/close) is `manage-flakes`' job.
