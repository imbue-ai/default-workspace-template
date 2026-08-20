---
name: report-incidental-flakes
description: Activate when a CI pipeline flaked and you need to re-run it. Best-effort -- before re-running, make sure the flake you just hit is tracked in the MIND backlog, so incidental flakes are not lost until the next scheduled sweep. Safe for coding/babysitting agents without Linear access; it degrades gracefully.
---

# Report an incidental flake

You hit a flake in a CI pipeline and are about to re-run it. Take that moment to make sure the flake is tracked, so the evidence survives your re-run. Best-effort: never let this block your real task.

You saw **one flake, not a full sweep**, so you have **no authority to close** flake tickets -- create or update only.

## Steps

### 1. Verify Linear access -- degrade gracefully without it

`latchkey services info linear` should report `"valid"`. If you cannot reach Linear (common for a coding agent):

- Still highlight the flake in your response and mark the test `@pytest.mark.flaky` -- the repo's `CLAUDE.md` already mandates both, with or without this skill.
- Note that Linear filing was skipped; the scheduled `detect-flakes` sweep will pick the flake up from CI later.
- Stop -- the rest of this skill needs Linear.

### 2. Confirm the flake is a pre-existing test, not your own code

Only flakes in code that already lives on `main` belong in the shared backlog. Check with git alone (no Linear):

- The failing test exists on `origin/main` -- you did not add or rename it on this branch.
- The failure is not attributable to changes this branch made to that test or to the code it exercises (`git diff origin/main -- <relevant paths>`).

If either check fails, the failure is most likely **your bug**, not a repo flake: do not file it -- debug it as part of your own work -- and stop here.

### 3. Hand the flake to manage-flakes as a partial set

Capture the evidence now, before the re-run replaces it: the failing test's id, the failure output, the branch CI ran on, and when. Assemble that as one flake record in the input shape the **`manage-flakes`** skill documents, then apply that skill, telling it this is a **partial** set -- one incidental flake, not a full window -- so it may **create or update tickets but never close**. It dedups against existing tickets and does the filing; read it for the rest.
