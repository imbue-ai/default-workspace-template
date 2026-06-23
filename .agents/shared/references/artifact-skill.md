# Artifact: skill

The artifact is a reusable skill under `.agents/skills/<name>/` -- a SKILL.md
process recipe plus scripts for its flow steps. Load this alongside
`harden-artifact.md` and your operation reference.

**Principle.** Reliability is the floor; simplicity is the target. Default to a
subcommand per cleanly-separable step plus a `run all` that chains them; add
surface beyond that only when a specific invariant demands it. Split into a
separate skill only when the components are likely to be used independently.

Consult `.agents/shared/references/spec-summary.md` for the agentskills.io
layout, the frontmatter template, PEP 723 script conventions, the
`[script]` / `[ai-script]` / `[prose]` step-kind definitions, and the scenario
template.

## Crystallize = reconstruct from the transcript

For the crystallize operation, the skill does **not** pre-exist -- you build it
from the lead's transcript and any handed-off `source_artifacts_dir`. So the
crystallize **reconstruct shape** applies: the outline gate and the final gate
both fire. (Update and heal operate on a skill that already exists on disk.)

## Outline fields (crystallize and emergent-update outline gate)

The outline you present at the `outline-approval` gate contains:

- A kebab-case skill name (naming rules in `spec-summary.md`).
- A one-paragraph description stating what the skill does AND when to use it
  (this becomes the SKILL.md `description` frontmatter).
- Inputs (CLI args if there's a script; prose parameters if agent-driven) and
  outputs (files, stdout, a report the agent hands back).
- A step-by-step flow, each step tagged `[script]` (deterministic),
  `[ai-script]` (model judgement scripted as a model call -- the default for any
  model step), or `[prose]` (user-in-the-loop). Use the re-run test: a step
  whose same prompt/criteria run every time with only the data varying is
  `[ai-script]`, not `[prose]`.
- Prose justification: tag `[prose]` only when the *user* must be in the loop
  while the skill runs; neither a model's judgement nor needing the conversation
  justifies it. Keep genuine prose at the edges, not wedged between scripted
  sections. A pure-prose skill (zero scripts) is valid only when every step is
  genuine executor meta-work.
- Subcommand structure: a subcommand per cleanly-separable step plus a `run all`
  that chains them. Note any step you keep inlined (e.g. it hands the next a
  live handle) and any subflow beyond the natural steps -- those need a specific
  invariant.
- 2-3 evaluation scenarios you plan to hand-craft, plus any edge cases you chose
  not to handle (and why).

For an **emergent update**, the outline also states the decision: update-in-place
of `<existing-name>`, or create-new-skill named `<new-name>` (see below).

## Update-in-place vs. create-new-skill (emergent update only)

See `.agents/shared/references/update-vs-create-new.md` for the full rubric.
Default to update-in-place; only split when the gap has a concrete standalone
use case. In the committed-update path the decision is already made by the diff.

For create-new-skill, build the new skill with the crystallize layout/validation
steps below using your approved outline; the old skill stays untouched.

## Building and editing

- Follow the layout and frontmatter conventions in `spec-summary.md`. A
  crystallized skill is marked `metadata.crystallized: true`.
- A new deterministic step goes in `scripts/`; a new model-judgement step is
  scripted as an `[ai-script]` model call in `scripts/`, not added as prose;
  only executor meta-work goes in SKILL.md as prose.
- Keep SKILL.md under ~500 lines; split long content into `references/`.
- **Heal**: the fix can be scripts, SKILL.md prose, or both. If the root cause
  was an ambiguous or wrong prose instruction, the fix is a SKILL.md edit even
  if the skill has scripts. A pure-prose skill (no scripts) heals by tracing the
  SKILL.md instructions against the incident inputs.
- **Cross-section alignment sweep** (after any localized edit): update the
  frontmatter `description`, the H1/opening prose, any top-of-file principle
  bullets, section headings, cross-references between sections, and
  `## Conventions` / `## Gotchas` -- every place that names or summarizes the
  changed material.

## Validation

```bash
uv run .agents/shared/scripts/validate_skill.py .agents/skills/<name>
```

This checks the structure and, when a `run.py` exists, runs `scripts/run.py
--help` to confirm its imports and PEP 723 dependencies resolve. It must print
`ok` before you move on.

## Scenarios and fixture tests

- Hand-craft 2-3 scenarios (happy path + realistic edge cases) using the
  template in `spec-summary.md`. Scenarios are **ephemeral** -- run them in your
  transcript, do NOT write them as files in the skill.
- For `[script]` / `[ai-script]` steps, invoke `scripts/run.py` with real inputs
  and inspect the output (an `[ai-script]` step makes a real Claude call; run it
  on a small input to note cost and confirm prompting works). For `[prose]`
  steps, walk through the SKILL.md instructions as if you were the executing
  agent and write out the walk-through.
- **Fixture-based tests for external-data parsing** (per `harden-artifact.md`):
  save 1-3 representative samples under `.agents/skills/<name>/tests/fixtures/`,
  add a `scripts/<name>_test.py` that feeds each fixture through the parser and
  asserts on the exact output shape, and run it.

## Data-capture guidance

The universal preserve-and-surface requirement -- persist each record's raw
payload and a source reference durably (under `runtime/<name>/`) -- is a
postcondition of any data-capture step you build. Two skill-specific points:
capture *all reasonable fields per record* in the calls you already make (not
just the fields the original turn displayed), and treat pagination as normal
when the ask requires it -- but do NOT make extra un-asked-for API calls just to
gather more data.

## Final-gate body templates

**Crystallize / update:**

```
<Built | Updated | Created> `<name>`:
- SKILL.md: <one-line summary, or "unchanged">
- Scripts: <one-line summary per script, or "none -- pure prose skill">
- Scenarios run: <list, with pass/fail>
- Shape changes from the sample: <none, or the output-schema / field / CLI /
  exit-code deltas a consumer or surface would need to adapt to>

Approve and save? (yes / no with notes)
```

**Heal:**

```
Fixed `<name>`:
- Root cause: <one-sentence>
- Change: <one-sentence>
- Scenarios run: <list, all pass>

Approve the fix? (yes / no with notes)
```

## Don't-touch

Nothing beyond the standard isolation rule (work on your own branch; the lead
merges). If the target is a built-in skill from the upstream template, note that
healing/updating it causes local drift to reconcile later via `update-self` or
`submit-upstream-changes`.
