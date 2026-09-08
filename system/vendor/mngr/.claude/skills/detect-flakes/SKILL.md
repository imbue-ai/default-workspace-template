---
name: detect-flakes
description: Sweep CI over a time window for every flaking test, then reconcile the whole MIND flake backlog. The scheduled/on-demand batch entry -- it hands the full flake set to manage-flakes and, having seen the full window, is the only path allowed to close stale tickets. Use to detect or triage CI flakes repo-wide; for a single flake you just hit, use report-incidental-flakes.
---

# Detect CI flakes (full-window sweep)

The batch entry point for flake reconciliation. It does two things: sweep CI for every test that flaked in a window, then hand the full set to the `manage-flakes` skill, which owns the clustering, prioritization, and Linear filing -- do not redo any of that here.

Because this path saw the whole window, it -- and only it -- may close stale tickets: a test absent from a full sweep has stopped flaking, whereas absence from a partial set (e.g. one reported via `report-incidental-flakes`) proves nothing.

## Arguments

`--autonomous` -- run unattended; the nightly `flake-sweep-scheduled.yml` job passes it. It
changes exactly two things and nothing else:

- `manage-flakes` applies its plan without pausing for approval (see that skill's step 5).
- The run summary must be written to `flake-sweep-summary.md` at the repo root.

Without it, behave exactly as before: stop for approval, and write no summary file.

**Unattended means there is no next turn.** The process exits the moment you end your turn, so
anything you background or promise to "report back on" is killed mid-flight and the job goes
*green* having done nothing. Run every step in the foreground and do not end your turn until
the reconciliation is finished. This is why the CI job sweeps for you (step 1): the sweep
outruns an agent's per-command timeout, so the only way an agent can run it is to background
it -- and then lose it. What is left for you is the judgement, which fits in one turn.

## Preconditions

- `gh` authenticated for the repo whose CI you are sweeping (the CLI's default unless overridden).
- `manage-flakes`' Linear precondition (latchkey), since you hand off to it.

## Steps

1. **Sweep.** Get the window's flakes into `/tmp/flakes.json`, then read that file -- each record is one flaky test with its evidence.

   **If `/tmp/flakes.json` already exists and is non-empty, a caller has swept for you: use it as-is and do not re-run the sweep.** The nightly CI job does exactly this, because the sweep outruns an agent's per-command timeout.

   Otherwise run it yourself (slow -- write to the file, then read the file):

   ```bash
   uv run python scripts/flake_reconcile.py list-flakes > /tmp/flakes.json
   ```

   The CLI's defaults are right for the routine sweep; adjust `--window-days` (or other flags -- see `--help`) only if asked.

2. **Reconcile.** Apply `manage-flakes` to the full set, stating that it came from a full-window sweep, what the window was, **and whether this run is autonomous**. The first authorizes closing stale tickets; the last authorizes applying without approval. Everything downstream (clustering, priorities, ticket create/update/close) is `manage-flakes`' job.

3. **Summarize (autonomous runs only).** `manage-flakes` writes `flake-sweep-summary.md` as it goes. Before finishing, confirm that file exists and reads as a standalone report: someone who sees only the Slack post must be able to tell what flaked, what changed in Linear, and what needs a human. The job fails if it is missing.
