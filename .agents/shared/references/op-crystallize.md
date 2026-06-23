# Operation: crystallize

The **create** operation. The artifact is coming into existence for the first
time and you are putting in the thorough pass that makes it real. Load this
alongside `harden-artifact.md` (the universal contract) and your artifact
reference (where it lives, how to test it, what not to touch).

There are two shapes, and your **artifact reference tells you which applies**:

- **Reconstruct** -- the artifact does not yet exist on disk; you build it from
  the lead's transcript and/or a handed-off sample. This is the skill case.
  Both the outline gate and the final gate apply.
- **Pre-existing, confirmed-live** -- the artifact already exists on disk and the
  user already signed off on its shape in the foreground (e.g. a scaffolded web
  service the lead built and the user clicked around). Nothing is reconstructed
  and there is no outline gate; the live confirmation stands in for the final
  gate. Harden it and report `done`.

## Valid report `name:` values

- Gates (reconstruct shape only): `outline-approval` (Stage 2),
  `final-artifact` (Stage 6).
- Terminal statuses: `done`, `stuck`.

In the pre-existing/confirmed-live shape, emit no gates; you may emit a
mid-flight `question` only if your artifact reference allows it, and you finish
with `done` or `stuck`.

## Stage 1: Reconstruct (reconstruct shape only)

1. Read the task file. The `## What was done` description and the `## Anchors`
   verbatim quotes are your primary guide.
2. Locate the work in the lead's transcript -- follow
   `.agents/shared/references/transcript-exploration.md`.
3. Research the relevant APIs, libraries, and existing utilities you will need.
   Prefer reusing existing functions over reimplementing.
4. If anything is unclear, add your question to the list you surface at Gate 1.

If your task frontmatter sets `source_artifacts_dir`, the calling skill has
pre-staged scripts and sample data at that path. Read those first so you reuse
working code instead of rebuilding from scratch. **You are not bound to the
sample's data shape** -- crystallization is exactly the moment to reconsider how
the task should be done, including improving the output shape, field names, or
structure. When your planned output differs from the handed-off sample, call
that out explicitly at the outline (Stage 2) and again at the final gate (Stage
6): the lead may have surfaces built on the old shape that need reconciling.

## Stage 2: Propose an outline, then Gate 1 (reconstruct shape only)

Produce a short outline. Your artifact reference defines the outline fields and
the layout conventions (for a skill: name, description, inputs/outputs,
step-by-step flow with each step tagged `[script]` / `[ai-script]` / `[prose]`,
subcommand structure, and 2-3 scenarios you plan to hand-craft). Include any
edge cases you foresaw but chose not to handle, and why.

Write a `type: gate`, `name: outline-approval` report whose body is the outline
plus an explicit "Approve this outline? (yes / no with notes)" prompt. Push it
and stop. If the user asks for changes, iterate and emit a fresh
`outline-approval` report. Do not proceed without an explicit yes.

## Stage 3: Build / harden the artifact

Build (reconstruct shape) or harden in place (pre-existing shape) per your
artifact reference's layout and validation steps. Apply the universal
testing/hardening and preserve-and-surface contract from `harden-artifact.md`.

## Stage 4: Scenarios

Hand-craft and run scenarios that exercise the artifact end-to-end (happy path
plus realistic edge cases). Your artifact reference gives the scenario specifics
(for a skill, the scenario template and the fixture-based tests for any external
data parsing). Fix the artifact when a scenario fails; fix the scenario when the
artifact is right but the scenario was wrong.

## Stage 5: Review gates

Run the review gates per `harden-artifact.md`, before the final gate report, so
the user sees a single report that already reflects the verdicts.

## Stage 6: Final gate, then commit and hand off

In the **reconstruct shape**, write a `type: gate`, `name: final-artifact`
report (your artifact reference gives the body template -- a built-artifact
summary, the scenarios run, and any shape changes from the sample) plus an
"Approve and save? (yes / no with notes)" prompt. Push it and stop. On approval,
commit on your branch and emit a `name: done` terminal report.

In the **pre-existing/confirmed-live shape**, skip the final gate: commit and
emit `name: done` once tests and gates pass. The user already confirmed the
shape live, and the lead reveals/refreshes after merge.

## If you need to give up

Emit a `stuck` terminal report per `harden-artifact.md`. For the reconstruct
shape, valid reasons include: the work had no stable process across hypothetical
re-runs (each re-run would need entirely different steps, not just different
data), or a dependency you cannot resolve. "Too judgement-heavy to script" is
not a valid reason -- model judgement that is a fixed part of the flow is
scripted, not abandoned.
