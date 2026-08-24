---
name: deflake-mngr-ci
description: Fix one CI flake end-to-end, from its filed Linear ticket to a draft PR -- the fixer counterpart to the flake-detection skills (detect-flakes / manage-flakes) that file these tickets. Reproduce it, find the true root cause, fix it at the source (never a weakened test, never a settle-then-assert band-aid), and pin it with a test. Invoke with /deflake-mngr-ci <ticket>.
---

# Deflake mngr CI

Carry one filed CI-flake ticket to a principled fix and a draft PR -- the fixer counterpart to the flake-detection skills (`detect-flakes` sweeps CI; `manage-flakes` files and manages the tickets). **Read `manage-flakes`** for the ticket's anatomy and split/merge mechanics rather than assuming them here. Your job: one ticket, one root cause, one honest fix.

## Prerequisites -- skills this one orchestrates

Invoke these; never reimplement them. If one is unavailable in your harness, install it from its home first:

- **`work-on-linear`**, **`crispy-comments`** -- vendored in this repo under `.claude/skills/`, public at `https://github.com/imbue-ai/mngr` (also home to this skill and the flake-detection skills).
- **`de-complect`** -- `https://github.com/danverbraganza/de-complect` (skill at `skills/de-complect/`).

## Procedure

1. **Adopt the ticket.** Invoke `/work-on-linear <ticket>`; it gates, claims, and branches the ticket, then stops without opening a PR. **This skill deliberately continues past that stop**: steps 2-8 drive the fix itself, ending in a draft PR.

2. **Reproduce, then find the *true* root cause -- agentically.** Never fix what you cannot explain. Read the code around the failing test; the CI logs, traces, and run output where the flake appeared (see `detect-flakes` for how that CI data is surfaced); and the commit history and branches the ticket cites (`git log` / `git blame`). Chase the failure to its actual source of nondeterminism -- a race, an ordering assumed but never established (a missing happens-before edge), a shared resource, an external dependency -- and prove it, ideally with a test that fails because of it. Treat any sleep, poll, or retry already in the path as a symptom, not an explanation: it marks where synchronization is missing, and the root cause usually lives exactly there.

3. **Split if it is really more than one bug.** If investigation reveals independent root causes bundled in the ticket, split it (per `manage-flakes`' mechanics) and continue this run on exactly one of them.

4. **Design a principled fix; reject band-aids.** Discard any approach that hides the symptom instead of removing the cause -- bumping a timeout, special-casing an error -- unless you can honestly justify it as a long-term improvement to the repo. Two shapes of "fix" are never justifiable:

   - **Never weaken the test.** Flaky or not, a test's purpose is to verify the code works; its entire value is its power to fail on broken code -- its sensitivity as an oracle. Loosening assertions, widening tolerances, retrying or sleeping around the assertion, skipping or xfail-ing -- each manufactures false negatives, trading the test's power to detect a real defect for a green run. Gates likewise: a gate that cannot verify its precondition must fail closed, never open -- failing open admits the very thing the gate exists to block. (`@pytest.mark.flaky` is not a weakening: the repo mandates it so the runner retries a known flake while its ticket is open -- triage to keep CI usable, never a fix, never your final answer.)

   - **Never settle-then-assert.** Polling for quiescence, arbitrary sleeps, retry-until-green, any "wait for the system to settle before validating" mechanism -- these paper over the missing happens-before edge (step 2) instead of establishing it. The correct fix establishes it explicitly: await the specific completion signal or observable condition, impose a barrier or causal ordering, inject a controllable clock, or remove the shared-mutable-state race at its source -- deterministic, not probabilistically settled. Wall-clock waiting is legitimate only when elapsed real time is genuinely part of the specification under test.

   For each surviving candidate, state how it leaves the repository durably better: nondeterminism removed at the source, an illegal state made unrepresentable, a contract tightened. Choose the strongest.

5. **Escalate genuine forks.** If two or more approaches are each defensible with real, differing tradeoffs, do not choose silently: lay out the options and their tradeoffs and let a human decide (use your harness's question or plan mechanism).

6. **De-complect the design.** Run `/de-complect` on the chosen approach before writing code.

7. **Implement in full repo compliance.** Re-read the repo's `CLAUDE.md`, `style_guide.md`, and relevant project docs -- follow them, not your memory of them. Make atomic commits. Add or strengthen a test that pins the flake: it fails before your fix and passes after. Verify through the repo's own test workflow, and satisfy everything it demands of a change (e.g. its changelog rule).

8. **Finish.** Run `/crispy-comments` on the diff, then push and open a **draft** PR per the repo's PR conventions (read those at the source too).

## Guardrails

- One root cause per ticket: if you find two, split (step 3) rather than fixing both under one ticket.
- No band-aids: a timeout bump or special case is acceptable only with an honest long-term justification (step 4).
- Never weaken the test: its whole value is its power to fail on broken code, and a gate must fail closed, never open (step 4).
- Never settle-then-assert: establish the missing happens-before edge instead of waiting out the nondeterminism (steps 2 and 4).
- Never silently pick between real tradeoffs -- escalate (step 5).
- Repo conventions and sibling skills drift: re-read them at the source every run, never from a remembered or restated copy.
