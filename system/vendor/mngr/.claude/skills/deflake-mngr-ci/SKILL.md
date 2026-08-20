---
name: deflake-mngr-ci
description: Fix one CI flake end-to-end, from its filed Linear ticket to a draft PR -- the fixer counterpart to the flake-detection skills (detect-flakes / manage-flakes) that file these tickets. Reproduce it, find the true root cause, fix it at the source (never a band-aid), and pin it with a test. Invoke with /deflake-mngr-ci <ticket>.
---

# Deflake mngr CI

Carry one filed CI-flake ticket to a principled fix and a draft PR -- the fixer counterpart to the flake-detection skills (`detect-flakes` sweeps CI; `manage-flakes` files and manages the tickets). **Read `manage-flakes`** for the ticket's anatomy and split/merge mechanics rather than assuming them here. Your job: one ticket, one root cause, one honest fix.

## Prerequisites -- skills this one orchestrates

Invoke these; never reimplement them. If one is unavailable in your harness, install it from its home first:

- **`work-on-linear`**, **`crispy-comments`** -- vendored in this repo under `.claude/skills/`, public at `https://github.com/imbue-ai/mngr` (also home to this skill and the flake-detection skills).
- **`de-complect`** -- `https://github.com/danverbraganza/de-complect` (skill at `skills/de-complect/`).

## Procedure

1. **Adopt the ticket.** Invoke `/work-on-linear <ticket>`; it gates, claims, and branches the ticket, then stops without opening a PR. **This skill deliberately continues past that stop**: steps 2-8 drive the fix itself, ending in a draft PR.

2. **Reproduce, then find the *true* root cause -- agentically.** Never fix what you cannot explain. Read the code around the failing test; the CI logs, traces, and run output where the flake appeared (see `detect-flakes` for how that CI data is surfaced); and the commit history and branches the ticket cites (`git log` / `git blame`). Chase the failure to its actual source of nondeterminism -- a race, an unstated ordering or timing assumption, a shared resource, an external dependency -- and prove it, ideally with a test that fails because of it.

3. **Split if it is really more than one bug.** If investigation reveals independent root causes bundled in the ticket, split it (per `manage-flakes`' mechanics) and continue this run on exactly one of them.

4. **Design a principled fix; reject band-aids.** Discard any approach that hides the symptom instead of removing the cause -- bumping a timeout, special-casing an error, retrying around nondeterminism -- unless you can honestly justify it as a long-term improvement to the repo. For each surviving candidate, state how it leaves the repository durably better: nondeterminism removed at the source, an illegal state made unrepresentable, a contract tightened. Choose the strongest.

5. **Escalate genuine forks.** If two or more approaches are each defensible with real, differing tradeoffs, do not choose silently: lay out the options and their tradeoffs and let a human decide (use your harness's question or plan mechanism).

6. **De-complect the design.** Run `/de-complect` on the chosen approach before writing code.

7. **Implement in full repo compliance.** Re-read the repo's `CLAUDE.md`, `style_guide.md`, and relevant project docs -- follow them, not your memory of them. Make atomic commits. Add or strengthen a test that pins the flake: it fails before your fix and passes after. Verify through the repo's own test workflow, and satisfy everything it demands of a change (e.g. its changelog rule).

8. **Finish.** Run `/crispy-comments` on the diff, then push and open a **draft** PR per the repo's PR conventions (read those at the source too).

## Guardrails

- One root cause per ticket: if you find two, split (step 3) rather than fixing both under one ticket.
- No band-aids: a timeout, retry, or special case is acceptable only with an honest long-term justification (step 4).
- Never silently pick between real tradeoffs -- escalate (step 5).
- Repo conventions and sibling skills drift: re-read them at the source every run, never from a remembered or restated copy.
