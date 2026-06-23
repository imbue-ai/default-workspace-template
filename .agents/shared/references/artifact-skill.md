# Artifact: skill

A reusable skill under `.agents/skills/<name>/` -- a SKILL.md process recipe plus
the scripts its steps call. This reference describes what a skill *is* and how you
author, validate, and test one.

**Principle.** Reliability is the floor; simplicity is the target. Default to a
subcommand per cleanly-separable step plus a `run all` that chains them; add
surface beyond that only when a specific invariant demands it. Split into a
separate skill only when the components are likely to be used independently.

Consult `.agents/shared/references/spec-summary.md` for the agentskills.io
layout, the frontmatter template, PEP 723 script conventions, the
`[script]` / `[ai-script]` / `[prose]` step-kind definitions, and the scenario
template.

## Where a skill's behavior lives

A skill's behavior is split between its scripts and its SKILL.md prose, so a
change (or a fix) may touch either or both:

- A deterministic step is a `[script]`; a model-judgement step is an
  `[ai-script]` model call -- both live in `scripts/`. Only executor meta-work
  belongs in SKILL.md as `[prose]`.
- A wrong behavior can originate in a script OR in an ambiguous/incorrect prose
  instruction; when the root cause is the prose, the edit is a SKILL.md edit even
  if the skill has scripts. A pure-prose skill (no scripts) has all of its
  behavior in SKILL.md.
- Keep SKILL.md under ~500 lines; split long content into `references/`.
- A crystallized skill is marked `metadata.crystallized: true`.
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
`ok`.

## Scenarios and fixture tests

- Hand-craft 2-3 scenarios (happy path + realistic edge cases) using the
  template in `spec-summary.md`. Scenarios are **ephemeral** -- run them in your
  transcript, do NOT write them as files in the skill.
- For `[script]` / `[ai-script]` steps, invoke `scripts/run.py` with real inputs
  and inspect the output (an `[ai-script]` step makes a real Claude call; run it
  on a small input to note cost and confirm prompting works). For `[prose]`
  steps, walk through the SKILL.md instructions as if you were the executing
  agent and write out the walk-through.
- **Fixture-based tests for external-data parsing**: save 1-3 representative
  samples under `.agents/skills/<name>/tests/fixtures/`, add a
  `scripts/<name>_test.py` that feeds each fixture through the parser and asserts
  on the exact output shape, and run it.

## Data capture

If a skill captures data, persist each record's raw payload and a source
reference durably (under `runtime/<name>/`) -- a postcondition of any
data-capture step. Two skill-specific points: capture *all reasonable fields per
record* in the calls you already make (not just the fields the original turn
displayed), and treat pagination as normal when the ask requires it -- but do NOT
make extra un-asked-for API calls just to gather more data.

## Built-in skills

Some skills are built-ins synced from the upstream template (`parent.toml`).
Editing one creates local drift to reconcile later; treat such an edit as a
change to shared infrastructure, not a private one.
