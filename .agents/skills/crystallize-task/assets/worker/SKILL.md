---
name: crystallize-task-worker
description: Turn a crystallization task (a description of work plus verbatim quote anchors that locate it in the lead's transcript) into a committed, reviewed, user-approved skill. Invoke when your task file asks you to crystallize a turn into a new skill.
metadata:
  role: worker-sub-skill
---

# Building a crystallized skill

Your task file describes a turn of work that should become a reusable skill
and gives you verbatim quote anchors for locating it in the lead's
transcript via `mngr transcript`. Follow these stages to go from
"task handed off" to "new skill committed on your branch".

**Follow `.agents/shared/references/crystallize-artifact.md` for the generic
crystallization contract** -- the bar, working in isolation, reporting back,
the testing/hardening contract, the review gates, preserving captured data, and
the give-up path. This sub-skill is the specialization where the artifact is a
**skill**: it adds transcript reconstruction (Stage 1), the agentskills.io
layout, scenario crafting, and an outline gate. The stages below give those
skill-specific parts; where one matches the generic contract it just points
back to it.

**Principle.** Reliability is the floor; simplicity is the target. Default to
a subcommand per cleanly-separable step plus a `run all` that chains them (see
`spec-summary.md`); add surface beyond that only when a specific invariant
demands it.

Consult `.agents/shared/references/spec-summary.md` for the agentskills.io
layout, frontmatter template, PEP 723 script conventions, and the scenario
template you will use in Stage 4.

## Reporting back to the lead

Follow `.agents/shared/references/worker-reporting.md` for the report-file
procedure and task-file frontmatter schema. Substitute:

- `<RUNTIME_REPORTS_DIR>` → `runtime/crystallize/reports/`
- `<TASK_FILE_GLOB>` → `runtime/crystallize/*/task.md`

Valid `name:` values for this worker:

- Gates: `outline-approval` (Stage 2), `final-artifact` (Stage 6).
- Terminal statuses: `done` (Stage 7), `stuck` (see "If you need to give up"
  below).

## Stage 1: Replicate

1. Read the task file. The `## What was done` description and the
   `## Anchors` verbatim quotes are your primary guide.
2. Locate the work being crystallized in the lead's transcript -- follow
   `.agents/shared/references/transcript-exploration.md`.
3. Research the relevant APIs, libraries, and existing utilities you will
   need. Prefer reusing existing functions over reimplementing.
4. If anything is unclear, add your question to the list you will surface
   in Gate 1.

## Stage 2: Propose an outline

Produce a short outline with:

- A kebab-case skill name (see the naming rules in
  `.agents/shared/references/spec-summary.md`).
- A one-paragraph description that states what the skill does AND when to
  use it (this becomes the SKILL.md `description` frontmatter field).
- Inputs: what the skill needs from its caller (CLI args if there's a
  script, or prose parameters if the skill is agent-driven).
- Outputs: what the skill produces (files, stdout, a report the agent
  hands back to the user).
- A step-by-step flow of the skill's process. Tag each step as one of the
  three kinds defined in `.agents/shared/references/spec-summary.md`:
  `[script]` (deterministic), `[ai-script]` (model judgement scripted as a
  model call -- the default for any model step), or `[prose]`
  (user-in-the-loop work). Use the re-run test: a step whose same
  prompt/criteria run every time with only the data varying is `[ai-script]`,
  not `[prose]`.
- Prose justification: apply the execution-mode test in `spec-summary.md`.
  Tag `[prose]` only when the user must be in the loop while the skill runs;
  neither a model's judgement nor needing the conversation justifies it. Keep
  any genuine prose at the edges, not wedged between two scripted sections.
- Subcommand structure: a subcommand per cleanly-separable step, plus a
  `run all` that chains them (see `spec-summary.md`). Note any step you keep
  inlined (e.g. it hands the next a live handle) and any subflow beyond the
  natural steps -- those need a specific invariant.
- A skill with zero `[script]`/`[ai-script]` steps (pure prose recipe) is
  valid only when every step is genuine executor meta-work -- do not invent
  scripts where judgement is the executor's, but do not park model
  judgement in prose to avoid scripting it.
- 2-3 evaluation scenarios you plan to hand-craft (happy path + edge cases).
- Any edge cases you foresaw but chose not to handle (and why).

**You are not bound to the sample's data shape.** The lead may hand you sample
data or scratch scripts (via `source_artifacts_dir`) that fix a particular
output schema, but crystallization is exactly the moment to reconsider how the
task should be done -- including improving the output shape, field names, or
structure. Changing it is allowed and expected; reliability and a clean design
win over matching the scratch shape. When your planned output differs from the
sample the lead handed off, **call that out explicitly in the outline** (and
again at Gate 2): the lead may have surfaces built on the old shape that need
reconciling, and they can only do that if you flag the delta.

### Gate 1: outline approval

Write a report with `type: gate`, `name: outline-approval`, and a body
that contains the outline plus an explicit "Approve this outline? (yes
/ no with notes)" prompt. Push it and stop, per the reporting procedure
above.

Body template:

```
Proposed skill outline:

<paste outline>

Approve this outline? (yes / no with notes)
```

If the user asks for changes, iterate, then emit a fresh
`type: gate, name: outline-approval` report with the revised outline.
Do not proceed to Stage 3 without an explicit yes.

## Stage 3: Build the artifact

Follow the layout and frontmatter conventions in
`.agents/shared/references/spec-summary.md`. Then validate:

```bash
uv run .agents/shared/scripts/validate_skill.py .agents/skills/<name>
```

This checks the structure and, when a `run.py` exists, runs
`scripts/run.py --help` to confirm its imports and PEP 723 dependencies
resolve. It must print `ok` before moving on. If it fails, fix and rerun.

### Data-capture guidance
The crystallization contract's preserve-and-surface requirement -- persist each
record's raw payload and a source reference durably (under `runtime/<name>/`) --
is a postcondition of any data-capture step you build. Two skill-specific points
on top: capture *all reasonable fields per record* in the calls you're already
making (not just the fields the user displayed in the original turn), so
downstream consumers stay unconstrained, and treat pagination as normal when the
ask requires it -- but do NOT make extra un-asked-for API calls just to gather
more data.

## Stage 4: Hand-craft and run scenarios

Pick 2-3 scenarios that exercise the skill end-to-end:

1. **Happy path**: the most common input shape.
2. **Edge case A**: a realistic non-happy input (empty, large, malformed).
3. **Edge case B** (optional): a second non-happy input exercising a
   different code path.

Use the scenario template in `.agents/shared/references/spec-summary.md` to
record each scenario in your transcript. Scenarios are *ephemeral* -- do NOT write
them as files in the skill.

Run each scenario:

- For `[script]` and `[ai-script]` steps: invoke `scripts/run.py` (or the
  relevant helper) with real inputs and inspect the output. An `[ai-script]`
  step makes a real Claude call; run it on a small input to note cost information
  and confirm that your prompting is functional.
- For `[prose]` steps: walk through the SKILL.md instructions as if you were
  an agent using the skill, and confirm they produce the expected
  behavior on the scenario's data. Write out this walk-through process; don't just think through it.

If a scenario fails, fix the skill (script or prose). If the skill is
correct but your scenario was wrong, update the scenario.

### Fixture-based tests for skills that parse external data

The crystallization contract requires fixture-based tests for anything that
parses external data (HTML, JSON from third-party APIs, scraped pages, uploaded
files) -- live-data scenarios alone miss the bugs that only surface when a
specific input shape hits the parser. For a skill, that means: save 1-3
representative samples under `.agents/skills/<name>/tests/fixtures/` (small,
anonymized if applicable), add a `scripts/<name>_test.py` that feeds each fixture
through the parser and asserts on the exact output shape (counts, field values,
edge-case flags), and run it as part of Stage 4.

## Stage 5: Code review and architecture verification

Run the review gates per the crystallization contract -- `/autofix` and
`/imbue-code-guardian:verify-architecture` on your branch -- and fix what they
flag. If verify-architecture flags non-blockers worth mentioning, surface them
in the Gate 2 summary below. Both run **before** Stage 6's final-artifact report,
so the user sees a single report that already reflects the verdicts rather than a
report-then-verify-then-report-again pattern.

## Stage 6: Gate 2 -- final artifact approval

Write a report with `type: gate`, `name: final-artifact`, and a body
containing the built-artifact summary plus an approval prompt. Push it
and stop, per the reporting procedure above.

Body template:

```
Built `<name>`:
- SKILL.md: <one-line summary>
- Scripts: <one-line summary per script, or "none -- pure prose skill">
- Scenarios run: <list, with pass/fail>
- Shape changes from the sample: <none, or list the output-schema / field /
  CLI / exit-code deltas a consumer or surface would need to adapt to>

Approve and save? (yes / no with notes)
```

## Stage 7: Commit and hand off

Commit on your current branch, then emit a `name: done` terminal report (body
shape per `.agents/shared/references/worker-reporting.md`). The lead will
merge the branch.

## If you need to give up

Emit a `stuck` terminal report per the crystallization contract; state in the
body that no skill was saved. Reasons that genuinely warrant giving up here:

- The work turned out to have no stable process across hypothetical re-runs --
  each re-run would require entirely different steps, not just different data.
- You hit a dependency you cannot resolve (e.g. a required service is
  unreachable, a file format you cannot parse).

Per the contract, "too judgement-heavy to script" is NOT a valid reason: model
judgement that is a fixed part of the flow is scripted as `[ai-script]` calls,
and only genuine executor meta-work stays as SKILL.md prose (a skill can be pure
prose if every step is executor meta-work). Only give up if the *process* itself
is unstable, not if parts of it happen to require judgement.
