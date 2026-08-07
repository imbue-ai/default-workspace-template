# Harness-specific instructions via `append_system_prompt`

## Summary

Retire codex's bespoke `.codex/AGENTS.md` -> `CODEX_HOME/AGENTS.md` copy and deliver
per-harness behavioral overrides through the one channel every harness already has:
`append_system_prompt`, set on the base `[agent_types.<harness>]` block in the
workspace's `.mngr/settings.toml`. One uniform mechanism, no per-harness prompt-file
plumbing, and the harness override rides the same parent-chain accumulation every role
already inherits.

## Motivation

Each harness's CLI ships built-in behaviors that are wrong *inside minds* -- tools whose
output never reaches the workspace chat, plan/goal stores the user cannot see, blocking
prompts rendered in a terminal nobody is watching. The fix is a short block of
harness-specific instructions ("do not use tool X; use `tk`").

Today only codex has a home for that block: a repo-committed `.codex/AGENTS.md` that
provisioning copies into codex's global-instructions slot (`CODEX_HOME/AGENTS.md`). The
other four harnesses (claude, pi, opencode, antigravity) have no such file -- their
harness-specific quirks either go unaddressed or leak into the shared project
`CLAUDE.md`/`AGENTS.md` that every harness reads (e.g. the "TodoWrite is disabled, use tk"
lines in the root `CLAUDE.md` are really claude-only).

Meanwhile, `append_system_prompt` was deliberately built for **all five** harnesses and
already flows to each harness's native system-prompt slot:

| Harness | `append_system_prompt` lands in | assembled by |
|---|---|---|
| codex | `config.toml` `developer_instructions` | `_build_developer_instructions` |
| claude | `--append-system-prompt` CLI flag | `_build_append_system_prompt_args` |
| pi | per-agent `APPEND_SYSTEM.md` | `_build_developer_instructions` / `_provision_append_system_prompt` |
| opencode | per-agent `AGENTS.md` (global rules) | `_build_agent_rules_text` |
| antigravity | per-agent `.gemini/GEMINI.md` (global rules) | `_build_agent_rules_text` |

So the harness-specific block does not need a new delivery path. It needs a *source*: a
`append_system_prompt__extend = ["..."]` entry on each base agent-type block. That is the
whole change.

## Confirmed: accumulation order

The blocking question was the order in which `append_system_prompt` blocks accumulate up
a template/parent chain. Traced through the config overlay engine:

- **List concat direction** (`node_merge.extend_aggregate_leaf`):
  `merged = list(current) + list(extend_payload)` -- the **lower-precedence** layer's list
  comes first, the **higher-precedence** layer's list is appended **after** it.
- **`__extend` over a base** (`node_merge.combine_nodes`): a higher `Extend` over a lower
  value resolves to `apply_extend(lower_payload, higher_payload)` = `lower + higher`. Same
  direction.
- **Parent-type inheritance** (`agent_config_registry._apply_custom_overrides_to_parent_config`):
  calls `merge_models_via_overlay(parent_config, custom_config)` with **parent as base**
  (lower) and **child as override** (higher). So a child's blocks append **after** the
  parent's.
- **Scope precedence** (system -> user -> project -> `-S` CLI): the higher-precedence
  scope is always the overlay `override`, appended after. Same direction throughout.
- **Within one harness's assembly** (e.g. codex `_build_developer_instructions`): the
  `append_system_prompt` blocks come first, then the `output_style` body **last** --
  "placing it last means it is the nearest instruction to the model."

### Net order the model sees

For a chain `base-harness -> mid-template -> most-derived-type`, plus an output style:

```
[ base-harness blocks ]     # e.g. [agent_types.codex] append_system_prompt__extend
[ mid-template blocks ]
[ most-derived blocks ]     # highest precedence -- LAST
[ output_style body ]       # nearest to the model
```

**Higher precedence = appended later = nearer the model.** The more-derived layer wins in
the sense these harnesses weight priority (last/nearest), *not* by being prefixed.

### Decision required (the one open design point)

Your ask was "the higher gets the prefixed/priority." Two readings, and they diverge:

1. **Priority = nearest to the model (last).** Then the **current behavior already does
   what you want** -- no change to ordering. The harness base block sits first (furthest,
   lowest priority), role/style overrides sit last (nearest, win). This is the natural
   reading for how LLMs weight instructions, and it is what all five assembly functions
   already implement.
2. **Priority = literally prefixed (first).** Then both the list-concat direction *and*
   each harness's assembly order would need to be reversed. This is a deeper change to
   shared config-engine semantics (`extend_aggregate_leaf`) that affects every
   `__extend` list in the config, not just prompts -- not advisable.

Recommendation: adopt reading (1). Put the harness base block on
`[agent_types.<harness>]` (lowest precedence, furthest from the model), and let roles and
output styles layer on top and win. If a specific instruction in the harness block *must*
override a role, that instruction is misplaced -- it belongs in the role, not the harness
base. Confirm (1) before implementation.

## Design

### 1. Source of the harness block: `.mngr/settings.toml`

Add a `append_system_prompt__extend` to each base agent-type block. Illustrative
(codex; content TBD -- see "Content" below):

```toml
[agent_types.codex]
# ...existing keys...
append_system_prompt__extend = ["""
# Codex-specific instructions

Do NOT use your built-in `update_plan`, `create_goal`/`get_goal`/`update_goal`, or
`request_user_input` tools -- their output/effects never reach the workspace chat. Use
`tk` for all plans and steps, and ask the user by writing an ordinary chat message.

The pytest-timeout note in the shared instructions (`PYTEST_MAX_DURATION_SECONDS`) refers
to your shell/exec tool's timeout.
"""]
```

Because every codex role (chat, worker, automation) resolves with `parent_type = "codex"`
-- or *is* `codex` -- and inheritance starts from the parent type's user config
(`resolve_agent_type` -> `parent_base_config`), this block reaches every codex agent
without being restated per role.

### 2. Remove the codex `.codex/AGENTS.md` mechanism

In `system/vendor/mngr/libs/mngr_codex`:

- `codex_config.py`: delete `get_repo_codex_instructions_path`, the
  `_GLOBAL_INSTRUCTIONS_FILENAME`/`get_codex_global_instructions_path` pair **iff** nothing
  else needs the global `AGENTS.md` slot (it does not, once the copy is gone), and the
  related module docstring paragraph.
- `plugin.py` `_provision_codex_home`: delete the block that reads
  `get_repo_codex_instructions_path` and writes `get_codex_global_instructions_path`
  (lines ~708-716). Leave `developer_instructions` (the `append_system_prompt` path)
  untouched -- that is now the sole channel.
- Delete the repo file `.codex/AGENTS.md`.
- Update/remove tests asserting the copy (`plugin_test.py`, any e2e that checks
  `CODEX_HOME/AGENTS.md`).

### 3. No code change for the other four harnesses

claude / pi / opencode / antigravity already assemble `append_system_prompt` into their
native slot. Adding the `[agent_types.<harness>]` block is config-only. Verify each
harness's assembly runs even when the role sets no `output_style` (it does: all four guard
on "no blocks -> write nothing", and a non-empty `append_system_prompt` is blocks).

### 4. Move claude's harness-only lines out of the shared instructions

The root `CLAUDE.md` currently carries claude-only overrides (TodoWrite disabled, `.claude`
symlink note, the pytest-timeout gloss). With the pattern in place, those move into
`[agent_types.claude] append_system_prompt__extend`, leaving the shared file harness-neutral.
This is the cleanup the whole change enables; do it in the same PR for claude, and audit the
shared files for any other harness-specific leakage.

## Content of each harness block (to be written)

One short markdown block per harness, each naming that harness's invisible/duplicative
built-ins and pointing at the workspace equivalent. Seeds:

- **codex**: `update_plan`, `create_goal`/`get_goal`/`update_goal`, `request_user_input`
  -> `tk` + chat message; shell-timeout gloss. **Note:** this workspace *already* disables
  `update_plan`, `features.goals`, and `experimental_request_user_input` via
  `config_overrides` on `[agent_types.codex]`, so the tool-disabling is belt-and-suspenders
  there; the prompt still usefully states the positive ("use tk"). For harnesses without a
  config kill-switch the prompt text is the only mechanism.
- **claude**: TodoWrite/ExitPlanMode/Task* already stripped via `cli_args
  --disallowed-tools`; the block reinforces "use tk", the memory-dir note, and the
  pytest-timeout gloss (migrated from `CLAUDE.md`).
- **pi / opencode / antigravity**: enumerate each harness's built-in plan/todo/goal/ask
  surfaces that the minds chat does not render, and redirect to `tk` + chat. Requires a
  per-harness audit of what built-in tools each exposes.

## Gotchas / invariants

- **Assign clobbers.** Any higher layer that sets `append_system_prompt` *without*
  `__extend` (a bare assign) drops the harness base block. Today the `chat` role sets
  `output_style`, not `append_system_prompt`, so the base survives -- but this is a
  standing constraint: every layer that contributes a prompt block must use
  `append_system_prompt__extend`, never a bare assign. Worth a comment in `settings.toml`
  and, ideally, a CLI-contract test that the resolved codex/claude/... config still
  contains the harness block.
- **Slot position shift (codex).** The block moves from codex's *global instructions*
  (concatenated before the project `AGENTS.md`) to `developer_instructions` (appended to
  codex's built-in instructions, a separate channel). Both are user/global-scope
  instruction text ahead of the conversation; behavior for these directives is
  equivalent, but it is a real position change -- call it out in the changelog.
- **TOML ergonomics.** The block is a multi-line triple-quoted TOML string living inside a
  config file. Acceptable for a short block; if any harness block grows large, revisit
  (e.g. a file-reference form for `append_system_prompt`) rather than embedding pages of
  prose in TOML.

## Files touched

- `.mngr/settings.toml` (dwt) -- add `append_system_prompt__extend` to the five
  `[agent_types.*]` blocks; comment the assign-clobber invariant.
- `CLAUDE.md` / `AGENTS.md` (dwt) -- remove migrated claude-only lines.
- `system/vendor/mngr/libs/mngr_codex/imbue/mngr_codex/{plugin,codex_config}.py` (mngr
  subtree) -- remove the `.codex/AGENTS.md` copy + helpers; update tests; changelog entry
  under `libs/mngr_codex/changelog/`.
- `.codex/AGENTS.md` (dwt) -- delete.

## Test plan

- Unit: `resolve_agent_type` for each harness yields a config whose
  `append_system_prompt` contains the harness block (and, for a child role, block order =
  parent-then-child).
- Unit (mngr_codex): `_provision_codex_home` no longer writes `CODEX_HOME/AGENTS.md`;
  `developer_instructions` carries the block.
- Manual: launch `+ New codex agent` and `+ New claude agent`, confirm each honors its
  block (codex does not surface a plan tool; claude uses tk), and confirm a role's
  `output_style` still applies on top.
