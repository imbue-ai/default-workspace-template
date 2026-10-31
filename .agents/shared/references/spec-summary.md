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
  data varies. Lives in the skill's `python/` package, run through an entry
  file in `.agents/skills/<name>/scripts/`.
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
  scripts/                  # optional; any shell scripts
    *.sh
  python/                   # the skill's uv project, whenever it has Python (see Packaging)
    pyproject.toml
    <name_with_underscores>_skill/
      __init__.py           # empty
      run.py                # the entry point, run by its own path
      *.py                  # the implementation, one module per concern
      *_test.py             # tests, beside the modules they cover
  references/*.md           # optional long-form docs; load on demand
  assets/...                # optional static resources (templates, samples)
```

A skill's Python lives in its own `python/` project, and any shell scripts in
its `scripts/` directory, i.e. `.agents/skills/<name>/scripts/`. The repo-root
`system/scripts/` is an unrelated place, for workspace provisioning and utility
scripts.

Start a new skill with the scaffold, which writes this layout, installs the
package and validates the result (`--python` for a skill with scripted steps;
`--crystallized` for one this lifecycle produces):

```bash
uv run --no-sync .agents/shared/scripts/agents_shared/scaffold_skill.py <name> \
    --description "<what-and-when>" --python --crystallized
```

This layout is for the workspace's own skills. A skill installed or copied in
from elsewhere (a skills marketplace, another repository, a download) keeps the
layout it came with: list it in `.agents/third-party-skills.toml` with where it
came from, and the root suite's checks on skills leave it out. Every skill not
listed there is held to them.

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

## The entry point, `run.py` (optional)

Include `run.py` when the skill has `[script]` or `[ai-script]` steps that
benefit from automation. A skill can be pure SKILL.md prose with no scripts
only when every step is `[prose]` executor meta-work; if any flow step is
deterministic or model-driven, it belongs in a script. Use scripts where
they earn their keep; don't force a script for genuine executor meta-work.

`run.py` is a module of the skill's package
(`python/<name_with_underscores>_skill/run.py`) with its own
`if __name__ == "__main__":` block. When you do include it, write the flow's logic as small helper
functions (one per step) in the skill's package (see Packaging below), and
have `run.py` expose them two ways:

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

A skill's Python lives in its own uv project, `python/`, a member of the root
workspace (the `.agents/skills/*/python` glob), so its dependencies resolve in
the one workspace lock and its package installs into the root venv:

- `python/pyproject.toml` names the project `<name>-skill` and its package
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
  Never leave a `python/` dir without its `pyproject.toml`: uv refuses a
  member directory without one, and that breaks every `uv` command in the
  workspace. A skill whose `scripts/` holds only shell scripts needs no
  `python/` at all.
- The code lives in the package, `run.py` included, and runs by its path from
  the root venv; it imports its siblings through the package
  (`from <name_with_underscores>_skill.parse import parse_rows`), so a new
  skill runs once `uv sync --all-packages` has installed it (the scaffold
  does). Tests sit in the package beside their modules and import them the
  same way. A new skill gets no entry stub in `scripts/`: everything that runs
  it is in the workspace and names the module's path from the start.
- Keep the entry cheap to start: it runs on every call, so its module imports
  only what every invocation needs at the top, and each subcommand imports its
  implementation (and any heavy library -- pydantic, loguru, click, httpx, ...)
  inside the function that runs it. A test fails an entry that loads one of
  those at import time. Code whose signatures name a heavy library's types
  goes in a module of its own, imported inside the subcommands that need it.
- A skill is a member of the shared workspace, so a dependency must co-resolve
  with the built-in packages'. After adding or changing one, run `uv lock` and
  then `uv sync --all-packages`, and commit `uv.lock` with the change; a clash
  surfaces at `uv lock`. A path dependency on a user lib (a standalone project
  under `system/libs`) pulls that lib's dependencies into the shared lock too.

A skill whose whole Python is one script that imports only the standard
library may instead be a **bare skill**: the script is
`scripts/<name_with_underscores>.py`, its test (if any) sits beside it as
`scripts/<name_with_underscores>_test.py` and imports it by that name, and there
is no `python/` dir and no PEP 723 header. It runs under the system `python3`
with no venv:

```bash
python3 .agents/skills/<name>/scripts/<name_with_underscores>.py <args>
```

It needs no `uv lock` or sync, and starts faster. The moment it needs a
dependency or a second module, move it into the `python/` project above.

See `.agents/shared/references/running-python.md` for why, and for the bare
tier (code the system `python3` runs) in general.

For [ai-script] steps, make sure to read and follow the instructions in the **`use-ai-integration`** skill.

- `argparse` entry point; no interactive prompts.
- Stateless across runs by default -- transient per-step I/O between
  subcommands is fine, durable cross-run state is not. If durable state is
  genuinely needed, flag it at Gate 2 -- don't invent a persistence scheme
  unilaterally.
- Fail loudly: exit non-zero on error, write the error to stderr.
- Document the invocation in SKILL.md:
  `uv run --no-sync .agents/skills/<name>/python/<name_with_underscores>_skill/run.py <args>`

## Validation

`uv run --no-sync .agents/shared/scripts/agents_shared/validate_skill.py <skill_dir>` checks SKILL.md
frontmatter, the kebab-case name rules, directory-name match, description
length and the 500-line body limit. For a bare skill it checks that the one
script imports only the standard library, carries no PEP 723 header, and that
`python3 -S -s <script> --help` (no site-packages) exits 0. When the skill has
Python otherwise (a `python/` dir, or a built-in skill's entry stub in
`scripts/`) it checks that `python/pyproject.toml` exists and names the project
`<name>-skill` and the package `<name_with_underscores>_skill`, that `uv lock
--check` passes (the workspace lock includes the skill's dependencies), and
that `uv run --no-sync <entry> --help` exits 0 for every package module with
its own `if __name__ == "__main__":` block (and any entry stub in `scripts/`) -- so a broken import or a missing dependency fails validation
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
- Command: `uv run --no-sync .agents/skills/<name>/python/<name_with_underscores>_skill/run.py <args>`
- Input: <stdin / files / env / CLI args>
- Expected: <exit code + stdout/file contents assertion>
- Actual: <observed>
- Status: pass | fail
```
