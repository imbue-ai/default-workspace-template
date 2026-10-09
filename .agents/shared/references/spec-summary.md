# agentskills.io layout cheat sheet

The crystallized skills in this project follow the [agentskills.io
spec](https://agentskills.io/specification). This file captures just the
bits you need when building or updating a skill; consult the spec directly
if anything else comes up.

## What a skill is

A skill is a SKILL.md describing a **process**, plus any supporting
scripts, references, or assets. The SKILL.md reads like a recipe: "do X,
then Y, then Z." Each step of that process is one of three kinds:

- **`[script]`** -- deterministic. Runs the same code every time, only the
  data varies. Lives in `.agents/skills/<name>/scripts/`.
- **`[ai-script]`** -- needs a model's judgement, but is a *fixed part of
  the flow* (the same prompt/criteria every run, only the data varies).
  Script it as an AI call following the `use-ai-integration` skill (see
  "Scripting a model step" below). This is the **default for any
  model-performed step** -- a step does not drop to prose just because it
  needs judgement.
- **`[prose]`** -- *user-in-the-loop work*: a step that needs the user
  present while the skill runs (see the test below). Written in SKILL.md as
  instructions the agent using the skill follows.

The point of `[ai-script]` is that the whole flow stays runnable headless:
once every flow step is scripted, the skill can be refreshed or scheduled
with no extra wiring. `[prose]` is reserved for work that genuinely needs the
executor in the loop -- never for a step that merely needs a model.

### The test: `[ai-script]` vs `[prose]`

Two things that feel like reasons for prose are not. **Needing a model's
judgement** isn't one -- that's exactly what `[ai-script]` is for. **Needing
the current conversation** isn't one either -- a script can fetch the
transcript and pass it into the prompt (this skill's own crystallize/update
workers run headless that way). So a script can assemble its own inputs, and
the decisive question is not about any single step but about the skill's
**execution mode**:

> Does this skill need the user *in the loop while it runs* -- for their live
> input (an approval, a decision, an answer to a clarifying question), or
> because they invoke it interactively to follow along and steer?

- **If no** -- the typical fetch / transform / judge skill, and the vast
  majority of cases -- every step is scriptable (`[script]` / `[ai-script]`)
  and the skill has no `[prose]`.
- **If yes** -- the steps that genuinely need the user are `[prose]`. You
  must be able to name which kind of involvement each needs; if you can't, it
  belongs in `[ai-script]`.

(Merely wanting to *watch* an automatable skill run is not a reason for
prose: with proper subcommand decomposition in the script, you can just choose
at runtime to go step by step if that's what the user seems to want)

### Push prose to the edges

Interactive involvement usually lands at the *edges* -- an approval or input
choice up front, a decision about the result at the back -- so the healthy
shape is **prose at the edges, scripted steps in the middle**. A `[prose]`
step wedged *between* two scripted sections is the expensive case: it splits
the pipeline into two halves that can't compose, which is what stops the flow
from running unattended. Only accept it when the user must genuinely intervene
mid-run (a mandatory sign-off before a destructive step, a human steering
what runs next); otherwise there is almost certainly an `[ai-script]` you
haven't written yet.

A mixed flow of all three kinds is the norm for useful skills.

## Directory layout

```
.agents/skills/<name>/
  SKILL.md                  # required; body <= 500 lines (progressive disclosure)
  scripts/                  # optional; include when there are deterministic steps
    pyproject.toml          # required whenever scripts/ exists (see Packaging)
    run.py                  # the entry point: a thin dispatcher into the package
    <name_with_underscores>_skill/
      __init__.py           # empty
      *.py                  # the implementation, one module per concern
      *_test.py             # tests, beside the modules they cover
  references/*.md           # optional long-form docs; load on demand
  assets/...                # optional static resources (templates, samples)
```

A skill's scripts live in the skill's own `scripts/` directory, i.e.
`.agents/skills/<name>/scripts/`. The repo-root `system/scripts/` is an
unrelated place, for workspace provisioning and utility scripts.

The `name` used in `.agents/skills/<name>/` must match the `name` field in
SKILL.md frontmatter (1-64 chars, lowercase letters/digits + single hyphens,
no leading/trailing or consecutive hyphens).

## SKILL.md frontmatter

Minimum required:

```yaml
---
name: <skill-name>              # must match parent directory
description: <what-and-when>    # 1-1024 chars; describe behavior + triggers
metadata:
  crystallized: true            # set for skills produced by this lifecycle
---
```

Omit `allowed-tools`, `license`, and `compatibility` unless you have a
specific reason to constrain or declare them -- the defaults are fine.

A skill whose scripts run under a secret file (through
`system/scripts/with_secrets.py`; see the `connect-external-service` skill)
declares it, so a template that ships the skill asks its adopter for exactly
those variables:

```yaml
secrets:
  - file: example                  # data/.secrets/example.env
    variables: [EXAMPLE_API_KEY]   # at least one; POSIX identifiers
    note: an Example API key from the account's settings page
```

## .agents/skills/<name>/scripts/run.py (optional)

Include `run.py` when the skill has `[script]` or `[ai-script]` steps that
benefit from automation. A skill can be pure SKILL.md prose with no scripts
only when every step is `[prose]` executor meta-work; if any flow step is
deterministic or model-driven, it belongs in a script. Use scripts where
they earn their keep; don't force a script for genuine executor meta-work.

When you do include `run.py`, write the flow's logic as small helper
functions (one per step) and expose them two ways:

- **A subcommand per step**, whenever the step's inputs and outputs serialize
  cleanly -- data, not live handles. (A step that hands the next one an open
  browser session, a DB connection, or a large in-memory object stays inlined;
  splitting it is not reasonable.) Once the steps are already helper functions
  the split is cheap, and it pays off twice: an agent running the skill in a
  chat turn can drive the steps one at a time for a rich per-step progress
  view, and each boundary leaves an inspectable intermediate output.
- **A `run all` subcommand** that chains the steps in-process -- just function
  calls, no serialization cost -- for headless and scheduled runs.

Both entry styles call the same helper functions, so the logic has one source
of truth. The per-step split *is* the structure; don't invent subflows beyond
the skill's natural steps.

If the process interleaves deterministic and model-judgement steps, script
*both* (the model steps become `[ai-script]` calls -- see below) so the whole
chain runs end-to-end.

### Packaging

A skill's `scripts/` dir is a uv project of its own, and a member of the root
workspace (the `.agents/skills/*/scripts` glob), so its dependencies resolve in
the one workspace lock and install into the root venv:

- `scripts/pyproject.toml` names the project `<name>-skill` and its package
  `<name_with_underscores>_skill`, built with hatchling:
  ```toml
  [project]
  name = "<name>-skill"
  version = "0.1.0"
  requires-python = ">=3.12"
  dependencies = ["rich>=13"]

  [build-system]
  requires = ["hatchling"]
  build-backend = "hatchling.build"

  [tool.hatch.build.targets.wheel]
  packages = ["<name_with_underscores>_skill"]
  ```
  A `scripts/` dir that holds only shell scripts still needs a
  `pyproject.toml` (a `[project]` table plus `[tool.uv] package = false`):
  uv refuses a member directory without one, and that breaks every `uv`
  command in the workspace.
- The code lives in the package; `run.py` (and any other entry file at the top
  of `scripts/`) is a thin dispatcher that imports it. Tests sit in the package
  beside their modules and import them normally
  (`from <name>_skill.parse import parse_rows`).
- Keep the entry cheap to start: it runs on every call, so its module imports
  only what every invocation needs at the top, and each subcommand imports its
  implementation (and any heavy library -- pydantic, loguru, click, httpx, ...)
  inside the function that runs it. A test fails an entry that loads one of
  those at import time; one that truly needs it at import declares it in
  `scripts/pyproject.toml` under
  `[tool.workspace-template.entry-points."run.py"]` as
  `heavy-imports = ["pydantic"]`.
- A dependency must co-resolve with the rest of the workspace, the same as an
  app's. After adding or changing one, run `uv lock` and then
  `uv sync --all-packages`, and commit `uv.lock` with the change; a clash
  surfaces at `uv lock`.

See `.agents/shared/references/running-python.md` for why, and for the bare
tier (scripts the system `python3` runs) that only built-in code uses.

For [ai-script] steps, make sure to read and follow the instructions in the **`use-ai-integration`** skill.

- `argparse` entry point; no interactive prompts.
- Stateless across runs by default -- transient per-step I/O between
  subcommands is fine, durable cross-run state is not. If durable state is
  genuinely needed, flag it at Gate 2 -- don't invent a persistence scheme
  unilaterally.
- Fail loudly: exit non-zero on error, write the error to stderr.
- Document the invocation in SKILL.md:
  `uv run --no-sync .agents/skills/<name>/scripts/run.py <args>`

## Validation

`uv run --no-sync .agents/shared/scripts/validate_skill.py <skill_dir>` checks SKILL.md
frontmatter, the kebab-case name rules, directory-name match, description
length and the 500-line body limit. When the skill has a `scripts/` dir it
also checks that `scripts/pyproject.toml` exists and names the project
`<name>-skill` and the package `<name_with_underscores>_skill`, that `uv lock
--check` passes (the workspace lock includes the skill's dependencies), and
that `uv run --no-sync <entry> --help` exits 0 for every entry file at the top
of `scripts/` -- so a broken import or a missing dependency fails validation
here rather than only at scenario time. (This is a shallow import check:
`--help` exercises top-level imports and the argparse wiring, not imports done
lazily inside subcommand bodies -- those are left to scenario testing.) Prints
`ok` and exits 0 on success; exits 1 with a clear error on failure.

## Scenario template

Scenarios are *ephemeral* -- they exist in your transcript for
reproducibility, not on disk. Do NOT save scenarios as files in the skill.
Record each scenario in the transcript in this form:

```
### Scenario: <one-line description>
- Command: `uv run --no-sync .agents/skills/<name>/scripts/run.py <args>`
- Input: <stdin / files / env / CLI args>
- Expected: <exit code + stdout/file contents assertion>
- Actual: <observed>
- Status: pass | fail
```
