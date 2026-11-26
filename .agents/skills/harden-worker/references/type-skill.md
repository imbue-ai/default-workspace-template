# Creation: skill

A reusable skill under `.agents/skills/<name>/` -- a SKILL.md process recipe plus
the scripts its steps call. `.agents/shared/references/spec-summary.md` is the
authority on the agentskills.io spec: directory layout, frontmatter, the
`[script]` / `[ai-script]` / `[prose]` step kinds, `run.py` packaging,
validation, and the scenario template. This reference adds only what that spec
cheat-sheet and the universal `harden-creation.md` contract don't already cover.

A new skill starts from the scaffold (`spec-summary.md`, "Directory layout"),
which writes the layout, installs the package and validates it:
`uv run --no-sync .agents/shared/scripts/agents_shared/scaffold_skill.py <name>
--description "..." --python --crystallized`.

**Design judgement.** Reliability is the floor; simplicity is the target. Default
to a subcommand per cleanly-separable step plus a `run all` that chains them; add
surface beyond that only when a specific invariant demands it. Split into a
*separate* skill only when the components are likely to be used independently.

## Contents

- Skills that ingest recurring data batches
- Where a skill's behavior lives
- Skills that drive an app
- Testing a skill
- Data capture
- Built-in skills

## Skills that ingest recurring data batches

When the skill's job is to turn batches of raw records (JSON/CSV/Parquet dumps,
periodic exports, API pulls) into a processed, queryable store -- especially
when batches keep arriving and overlap -- build its pipeline by following the
`data-pipeline-builder` skill (`.agents/skills/data-pipeline-builder/`). The
pipeline is part of this skill, not a separate creation:

- All of its modules (`sources.py`, `store.py`, ...) go in the skill's package,
  `.agents/skills/<name>/python/<name_with_underscores>_skill/`, with its
  dependencies in `python/pyproject.toml` and `run.py` in the package as its
  entry point (layout and rules: `spec-summary.md`, "Packaging").
- Its tests go in the package too: where data-pipeline-builder says
  `tests/test_parse.py`, write `.agents/skills/<name>/python/<name_with_underscores>_skill/test_parse.py`.
  pytest imports it as `<name_with_underscores>_skill.test_parse`, so the name cannot collide
  with another skill's.
- Fixtures go in `.agents/skills/<name>/tests/fixtures/` as usual.

## Where a skill's behavior lives

A skill's behavior is split between its scripts (`[script]` / `[ai-script]`, in
its `python/` package) and its
SKILL.md prose, so a change -- or a
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

## Skills an app owns

A skill that exists only as part of one app lives in that app's directory, at
`system/apps/<package>/skills/<name>/`, and `.agents/skills/<name>` is a link
to it that `app-install` generates. Such a skill is part of the app, not a
creation of its own:

- It is in the app's footprint by where it is, with no reference to declare.
  `skill:<name>` resolves through the link to the app: the scope file carries
  the app's footprint, and the pass follows `type-app.md`. A pass that names
  both the app and the skill is refused, since they are one creation.
- Edit its files under the app's directory. Git reports a change there, and
  reports the link as one path, never a change through it.
- It has no `python/` project of its own. Code it needs lives in the app's
  package, reached as one of the app's console scripts or over the app's HTTP
  API.

The reverse lookup below tells the two kinds apart: for a skill an app owns,
the line's `path` is the link itself (`.agents/skills/<name>`) and its `note`
says the app carries the skill. The rest of this reference is about a skill
with a directory of its own under `.agents/skills/`.

## Skills that drive an app

A skill that calls an app's routes, its CLI, or its store belongs to that app's
footprint, and the app's references are where the link lives: one
`[[references]]` entry whose `path` is `.agents/skills/<name>` and whose `note`
names the surface used ("calls `POST /api/ingest`; writes
`data/.apps/<name>/`"). A built-in app keeps its references in
`system/apps/<package>/app.toml`. An app the workspace built or installed
keeps them in its record, `system/config/apps/<app>.toml`, which `app-install`
keeps; `[[references]]` in that app's `app.toml` is not read. The link is
one-directional -- the skill declares nothing about the app -- so check for
the entry by reverse lookup:

```bash
uv run --no-sync app-manifest references --for-path .agents/skills/<name>
```

One JSON line per app that claims the skill; no output means none does. When
the entry is missing, add it to a built-in app's `app.toml` as part of this
change. For an app with a record, record it with the installer, never by
editing the record or the manifest:

```bash
uv run --no-sync app-install reference add <app> .agents/skills/<name> --note "<the surface the skill uses>"
```

It writes the entry into the app's record and commits that one file on your
branch (`reference_added`; `refused` with the reason in `detail` when the
record already lists the path). Regenerate the scope file afterwards: the
record then sits under the skill's `context`, so the commit counts as inside
the footprint. When the skill stops using an app,
`uv run --no-sync app-install reference remove <app> .agents/skills/<name>`
takes the entry out. List what you registered in your report under
`References registered:`.

The skill's `context` in the scope file lists the primary directory of every app
that claims this skill, and the record of each one that has a record. Read
those to verify the invocation contract the skill
relies on -- the routes in `src/<package>/runner.py`, the CLI entry points in
`pyproject.toml`, `app.toml` -- and read them only: the reference entry
above is the sole edit this run makes to an app, and a fix that
belongs to the app itself is that app's own pass.

## Testing a skill

- While you iterate, run the skill's own tests by path. The root pytest config
  recurses into `.agents/`, so a bare root `uv run --no-sync pytest` would collect the
  entire monorepo to reach them:

  ```bash
  uv run --no-sync pytest .agents/skills/<name>
  ```

  The gate is `harden-creation.md`'s "The test gate": when the reverse lookup
  above found an app that claims this skill, `select-tests` runs that app's
  suite too, since it exercises the surface the skill calls.
- Validate with `uv run --no-sync .agents/shared/scripts/agents_shared/validate_skill.py
  .agents/skills/<name> --no-exemptions` -- it must print `ok` (see `spec-summary.md` for what it
  checks). A new skill passes every check. For a skill that existed before your pass, pass
  `--edited` in place of `--no-exemptions`: when its frontmatter carries a `predates-checks`
  exemption, restructure it until those checks pass and remove the exemption
  (`docs/system/skill_exemptions.md`); an `awaiting-restructure` one belongs to a
  built-in skill and stays, so leave that skill's structure alone.
- Hand-craft and run 2-3 scenarios (template in `spec-summary.md`); they are
  **ephemeral** -- run them in your transcript, never saved as files. For a
  `[script]` / `[ai-script]` step, invoke `.agents/skills/<name>/python/<name_with_underscores>_skill/run.py`
  on real input and inspect the output (an `[ai-script]` step makes a real
  Claude call -- run it on a small input to note cost). For a `[prose]` step,
  walk the SKILL.md instructions as the executing agent.
- The universal fixture-test rule (`harden-creation.md`), for a skill: save 1-3
  samples under `.agents/skills/<name>/tests/fixtures/` and add a
  `.agents/skills/<name>/python/<name_with_underscores>_skill/<name_with_underscores>_test.py` that feeds each through the
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
