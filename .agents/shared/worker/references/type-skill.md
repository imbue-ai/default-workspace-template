# Creation: skill

A reusable skill under `.agents/skills/<name>/` -- a SKILL.md process recipe plus
the scripts its steps call. `.agents/shared/references/spec-summary.md` is the
authority on the agentskills.io spec: directory layout, frontmatter, the
`[script]` / `[ai-script]` / `[prose]` step kinds, `run.py` packaging,
validation, and the scenario template. This reference adds only what that spec
cheat-sheet and the universal `harden-creation.md` contract don't already cover.

**Design judgement.** Reliability is the floor; simplicity is the target. Default
to a subcommand per cleanly-separable step plus a `run all` that chains them; add
surface beyond that only when a specific invariant demands it. Split into a
*separate* skill only when the components are likely to be used independently.

## Skills that ingest recurring data batches

When the skill's job is to turn batches of raw records (JSON/CSV/Parquet dumps,
periodic exports, API pulls) into a processed, queryable store -- especially
when batches keep arriving and overlap -- build its pipeline by following the
`data-pipeline-builder` skill (`.agents/skills/data-pipeline-builder/`). The
pipeline is part of this skill, not a separate creation:

- All of its modules (`sources.py`, `store.py`, ...) go in
  `.agents/skills/<name>/scripts/` beside `run.py`, running from the root venv.
  Do NOT add a nested `pyproject.toml` or per-skill dependencies.
- The root pytest config recurses into `.agents/`, so name test files for the
  skill -- where data-pipeline-builder says `tests/test_parse.py`, write
  `.agents/skills/<name>/scripts/<name>_parse_test.py` instead. A generic name
  like `test_parse.py` collides across skills.
- Fixtures go in `.agents/skills/<name>/tests/fixtures/` as usual.

## Where a skill's behavior lives

A skill's behavior is split between its scripts (`[script]` / `[ai-script]`, in
`.agents/skills/<name>/scripts/`) and its SKILL.md prose, so a change -- or a
fix -- may touch either or both. When a wrong behavior traces to an ambiguous
or incorrect prose instruction, the edit is a SKILL.md edit even if the skill
has scripts; a pure-prose skill (no scripts) has all of its behavior in
SKILL.md.

- A crystallized skill is marked `metadata.crystallized: true`.
- Keep SKILL.md under ~500 lines; split long content into `references/`.
- **Cross-section alignment sweep** (after any localized edit): update the
  frontmatter `description`, the H1/opening prose, any principle bullets, section
  headings, cross-references, and `## Conventions` / `## Gotchas` -- every place
  that names or summarizes the changed material.

## Skills that drive an app

A skill that calls an app's routes, its CLI, or its store belongs to that app's
footprint, and the app's manifest is where the link lives: one `[[references]]`
entry in `system/apps/<package>/app.toml` whose `path` is
`.agents/skills/<name>` and whose `note` names the surface used ("calls `POST
/api/ingest`; writes `data/.apps/<name>/`"). The link is one-directional -- the
skill declares nothing about the app -- so check for the entry by reverse
lookup and add it to the app's `app.toml` as part of this change when it is
missing:

```bash
uv run app-manifest references --for-path .agents/skills/<name>
```

One JSON line per app that claims the skill; no output means none does.

The scope file's `context` lists the primary directory of every app that claims
this skill. Read those to verify the invocation contract the skill relies on --
the routes in `src/<package>/runner.py`, the CLI entry points in
`pyproject.toml`, `app.toml` -- and read them only: the `[[references]]` entry
above is the sole edit this run makes inside an app's directory, and a fix that
belongs to the app itself is that app's own pass.

## Testing a skill

- Run the skill's own tests by path. The root pytest config recurses into
  `.agents/`, so a bare root `uv run pytest` would collect the entire monorepo
  to reach them:

  ```bash
  uv run pytest .agents/skills/<name>
  ```

  If the reverse lookup above found an app that claims this skill, that app's
  suite exercises the surface the skill calls, so run it too:
  `cd system/apps/<package> && uv run pytest`.
- Validate with `uv run .agents/shared/scripts/validate_skill.py
  .agents/skills/<name>` -- it must print `ok` (see `spec-summary.md` for what it
  checks).
- Hand-craft and run 2-3 scenarios (template in `spec-summary.md`); they are
  **ephemeral** -- run them in your transcript, never saved as files. For a
  `[script]` / `[ai-script]` step, invoke `.agents/skills/<name>/scripts/run.py`
  on real input and inspect the output (an `[ai-script]` step makes a real
  Claude call -- run it on a small input to note cost). For a `[prose]` step,
  walk the SKILL.md instructions as the executing agent.
- The universal fixture-test rule (`harden-creation.md`), for a skill: save 1-3
  samples under `.agents/skills/<name>/tests/fixtures/` and add a
  `.agents/skills/<name>/scripts/<name>_test.py` that feeds each through the
  parser and asserts the exact output shape.

## Data capture

Beyond the universal preserve-and-surface rule (`harden-creation.md`), persist
each record under `data/.skills/<name>/`, capture *all reasonable fields per record*
in the calls you already make (not just the fields the original turn displayed),
and treat pagination as normal when the ask requires it -- but do NOT make extra
un-asked-for API calls just to gather more data.

## Built-in skills

Some skills are built-ins synced from the upstream template (`system/config/parent.toml`).
Editing one creates local drift to reconcile later; treat such an edit as a
change to shared infrastructure, not a private one.
