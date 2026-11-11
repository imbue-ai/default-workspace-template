# Building a workspace skill: packaging and vocabulary

What this workspace adds to the [agentskills.io spec](https://agentskills.io/specification):
how a skill's steps are scripted, how its Python is packaged and validated, and
the words its prose uses. How to write the skill itself (SKILL.md size and
order, references, descriptions, version-bound text, paths, exemptions) is in
`docs/system/skill_authoring_guide.md`; read it before writing or restructuring
a skill. `.agents/shared/references/user-facing-language.md` stays the authority
for words the user reads.

## Contents

- Step kinds
- Directory layout
- SKILL.md frontmatter
- The entry point, `run.py`
- Packaging
- Validation
- Scenario template
- Glossary

## Step kinds

A SKILL.md describes a process, and each step of it is one of three kinds:

- **`[script]`**: deterministic. The same code every run; only the data varies.
- **`[ai-script]`**: a model's judgement as a fixed part of the flow (the same
  prompt and criteria every run). Script it as an AI call, following the
  `use-ai-integration` skill. This is the default for any step a model performs.
- **`[prose]`**: a step that needs the user present while the skill runs.
  Written in SKILL.md as instructions for the agent using the skill.

Once every flow step is scripted the skill runs headless, so it can be
refreshed or scheduled with no extra wiring. Two things that feel like reasons
for prose are not: needing a model's judgement (that is `[ai-script]`), and
needing the current conversation (a script can fetch the transcript and pass it
in). The deciding question is about the skill, not a step:

> Does this skill need the user in the loop while it runs: for an approval, a
> decision or an answer, or because they invoke it to follow along and steer?

- **No** (the usual fetch, transform, judge skill): every step is `[script]` or
  `[ai-script]`.
- **Yes**: the steps that need the user are `[prose]`. Name the kind of
  involvement each needs; a step you cannot name one for is `[ai-script]`.

Wanting to watch a skill run is not a reason for prose: per-step subcommands
let an agent go step by step at run time.

Keep prose at the edges (an approval up front, a decision about the result at
the end) and scripted steps in the middle. A `[prose]` step between two
scripted sections splits the pipeline into halves that cannot run unattended;
accept it only when the user must intervene mid-run.

## Directory layout

```
.agents/skills/<name>/
  SKILL.md                  # required
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
its own `scripts/` directory. The repo-root `system/scripts/` is an unrelated
place, for workspace provisioning and utility scripts.

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

The `name` in `.agents/skills/<name>/` must match the `name` field in SKILL.md
frontmatter (1-64 chars, lowercase letters and digits with single hyphens, no
leading, trailing or consecutive hyphens).

## SKILL.md frontmatter

Minimum required:

```yaml
---
name: <skill-name>              # must match parent directory
description: <what-and-when>    # 1-1024 chars; the guide gives its shape
metadata:
  crystallized: true            # set for skills produced by this lifecycle
---
```

Omit `allowed-tools`, `license`, and `compatibility` unless you have a specific
reason to constrain or declare them.

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

## The entry point, `run.py`

Include `run.py` when the skill has `[script]` or `[ai-script]` steps. A skill
is pure SKILL.md prose only when every step is `[prose]`.

`run.py` is a module of the skill's package
(`python/<name_with_underscores>_skill/run.py`) with its own
`if __name__ == "__main__":` block. Write the flow's logic as small helper
functions, one per step, and have `run.py` expose them two ways:

- **A subcommand per step**, whenever the step's inputs and outputs serialize
  cleanly (data, not live handles; a step that hands the next one an open
  browser session or a DB connection stays inlined). An agent running the skill
  in a chat turn drives the steps one at a time, and each boundary leaves an
  inspectable intermediate output.
- **A `run all` subcommand** that chains the steps in-process, for headless and
  scheduled runs.

Both call the same helper functions. Do not invent subflows beyond the skill's
natural steps. For `[ai-script]` steps, read and follow the
`use-ai-integration` skill.

- `argparse` entry point; no interactive prompts.
- Stateless across runs by default. Transient per-step I/O between subcommands
  is fine; durable cross-run state is not. If it is needed, flag it at Gate 2
  rather than inventing a persistence scheme.
- Fail loudly: exit non-zero on error, write the error to stderr.
- Document the invocation in SKILL.md:
  `uv run --no-sync .agents/skills/<name>/python/<name_with_underscores>_skill/run.py <args>`

## Packaging

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
  implementation (and any heavy library: pydantic, loguru, click, httpx) inside
  the function that runs it. A test fails an entry that loads one of those at
  import time. Code whose signatures name a heavy library's types goes in a
  module of its own, imported inside the subcommands that need it.
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
`.agents/shared/references/running-python.md` says why, and covers the bare
tier (code the system `python3` runs) in general.

## Validation

```bash
uv run --no-sync .agents/shared/scripts/agents_shared/validate_skill.py .agents/skills/<name> --no-exemptions
```

Run it whenever you create or edit a skill. It prints `ok` and exits 0, or
exits 1 with what to fix. It checks:

- **Frontmatter**: the name rules and directory match, and the description's
  length.
- **The guide's measurable rules**: `size`, `reachability`, `depth`, `paths`
  and `contents`. A failure states the goal; restructure the skill to meet it.
- **Exemptions**: `--no-exemptions` refuses one, which is the mode for a new
  skill and for any skill you edit. Without the flag, a skill passes with the
  exemption its frontmatter carries, and a stale one fails.
- **Python**: a bare skill's one script imports only the standard library,
  carries no PEP 723 header, and `python3 -S -s <script> --help` exits 0. A
  packaged skill's `python/pyproject.toml` names the project and package as
  above, `uv lock --check` passes, and `uv run --no-sync <entry> --help` exits
  0 for every package module with its own `if __name__ == "__main__":` block
  (and any entry stub in `scripts/`). `--help` exercises top-level imports and
  the argparse wiring only; imports inside subcommand bodies are left to
  scenario testing.

`validate_skill.py --stamp` is not for a skill you are working on. The flows
that bring existing skills under the checks (an update, a migration, adopting
a template) run it to record what each existing skill fails.

## Scenario template

Scenarios are ephemeral: they exist in your transcript for reproducibility, not
on disk. Do not save scenarios as files in the skill. Record each in the
transcript in this form:

```
### Scenario: <one-line description>
- Command: `uv run --no-sync .agents/skills/<name>/python/<name_with_underscores>_skill/run.py <args>`
- Input: <stdin / files / env / CLI args>
- Expected: <exit code + stdout/file contents assertion>
- Actual: <observed>
- Status: pass | fail
```

## Glossary

The words this workspace's skills use for its own things. One term per thing,
in every file an agent reads. The guide's glossary covers the authoring terms
(skill, reference, lead, worker); words shown to the user follow
`.agents/shared/references/user-facing-language.md`.

| Term | Meaning | Not |
|---|---|---|
| creation | Something built for the user here: an app, a skill, data, or a customization of one. | artifact |
| app | A creation the user opens as a window: a supervisord program with an `app.toml`, under `system/apps/`. | site, view, web service |
| service | A supervisord program with no window, under `system/services/`. | daemon, background app |
| program | A supervisord `[program:...]` entry, when the entry itself is meant; an app or a service otherwise. | |
| built-in skill | A skill that ships with the template. | system skill |
| crystallized skill | A skill the harden pass made from work done in a chat. | |
| automation | A skill that runs on a schedule. | cron job |
| routine | Anything that runs on a schedule and exits, made with `manage-routines`. | |
| template | A published, bootable snapshot of creations that another workspace can adopt. | inspiration |
| harden pass | One background run of the harden worker over a change. Shortened to "pass". | run, flow |
| operation | What a harden pass does: `crystallize`, `update` or `heal`. | |
| gate | A point where a worker stops for its lead's answer. | checkpoint |
| go-live | Making a hardened change the one that is running. | deploy |
| update | Moving the workspace to a newer release of the template, with `update-self`. | sync |
| release | A tagged version of the template. | |
| critical app | The shell, the chat, the terminal, or any app whose `app.toml` sets `critical = true`. | |
| shell | The top-level workspace UI, `system/apps/system_interface/`. | |
