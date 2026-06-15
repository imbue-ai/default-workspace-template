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
  data varies. Lives in `scripts/`.
- **`[ai-script]`** -- needs a model's judgement, but is a *fixed part of
  the flow* (the same prompt/criteria every run, only the data varies).
  Script it as an AI call following the `use-ai-integration` skill (see
  "Scripting a model step" below). This is the **default for any
  model-performed step** -- a step does not drop to prose just because it
  needs judgement.
- **`[prose]`** -- *executor meta-work* that is not part of an automated
  run: choosing inputs, interpreting the final result and deciding what to
  do next, user approval/interaction, anything that needs the live
  conversation context. Written in SKILL.md as instructions the agent using
  the skill follows.

The point of `[ai-script]` is that the whole flow stays runnable headless:
when every flow step (deterministic or model-driven) is scripted, the skill
can be refreshed or scheduled with no additional wiring. Prose is reserved
for the work that genuinely needs the executor in the loop -- not for any
step that happens to require a model. When you leave a model step as
`[prose]`, you must be able to say *why* a scripted model call
cannot do it.

A mixed flow of all three kinds is the norm for useful skills.

## Directory layout

```
.agents/skills/<name>/
  SKILL.md                  # required; body <= 500 lines (progressive disclosure)
  scripts/
    run.py                  # optional; include when there are deterministic steps
    *.py                    # optional helpers
  references/*.md           # optional long-form docs; load on demand
  assets/...                # optional static resources (templates, samples)
```

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

## scripts/run.py (optional)

Include `run.py` when the skill has `[script]` or `[ai-script]` steps that
benefit from automation. A skill can be pure SKILL.md prose with no scripts
only when every step is `[prose]` executor meta-work; if any flow step is
deterministic or model-driven, it belongs in a script. Use scripts where
they earn their keep; don't force a script for genuine executor meta-work.

When you do include `run.py`, keep it as simple as the invariants allow
-- default to a single entry point and one flow, and only add subcommands
or subflows when a specific invariant demands the separation.

If the process interleaves deterministic steps with model-judgement steps,
script *both*: the model-judgement steps become `[ai-script]` calls (below)
so the whole chain runs end-to-end without the executor in the loop. Only
break the flow into separate subcommands when a `[prose]` executor step
genuinely sits between two scripted sections.

### Scripting a model step (`[ai-script]`)

Script the step as a Claude call following
[calling-claude.md](calling-claude.md) -- don't default to one mechanism. It
picks the path by agency and whether `ANTHROPIC_API_KEY` is set (keyed
non-agentic -> `litellm`; keyless -> the copyable `claude_p.py` helper;
agentic -> `claude_p_task`) and covers surfacing the cost to the user.

Packaging: `run.py` stays an ordinary self-contained PEP 723 script. For the
keyless / agentic paths, copy `claude_p.py` (from the `use-ai-integration`
skill's `scripts/`) in beside `run.py` and list its deps (`anyio`,
`pydantic`) in the header; the keyed `litellm` path needs only `litellm`:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["anyio", "pydantic>=2"]   # or ["litellm>=1.88.1"] for the keyed path
# ///
import anyio
from claude_p import claude_p_completion   # keyless path; the file you copied in

async def main() -> None:
    result = await claude_p_completion(
        prompt,
        system="<a real task instruction, not a placeholder>",
        model="claude-haiku-4-5",
    )
    print(result.text, result.cost_usd)

anyio.run(main)
```

- Begin every `run.py` with a PEP 723 header pinning its inline deps:
  ```python
  # /// script
  # requires-python = ">=3.11"
  # dependencies = ["rich>=13"]
  # ///
  ```
- `argparse` entry point; no interactive prompts.
- Stateless by default. If persisted state is genuinely needed, flag it at
  Gate 2 -- don't invent a persistence scheme unilaterally.
- Fail loudly: exit non-zero on error, write the error to stderr.
- Document the invocation in SKILL.md:
  `uv run .agents/skills/<name>/scripts/run.py <args>`

## Validation

`uv run .agents/shared/scripts/validate_skill.py <skill_dir>` checks SKILL.md
frontmatter, the kebab-case name rules, directory-name match, description
length, 500-line body limit, and that any `run.py` begins with a PEP 723
header. Prints `ok` and exits 0 on success; exits 1 with a clear error on
failure.

## Scenario template

Scenarios are *ephemeral* -- they exist in your transcript for
reproducibility, not on disk. Do NOT save scenarios as files in the skill.
Record each scenario in the transcript in this form:

```
### Scenario: <one-line description>
- Command: `uv run .agents/skills/<name>/scripts/run.py <args>`
- Input: <stdin / files / env / CLI args>
- Expected: <exit code + stdout/file contents assertion>
- Actual: <observed>
- Status: pass | fail
```
