# Kanpan prompted commands

## Overview

Kanpan custom commands are fixed shell strings: a keybinding runs exactly the command written in config, parameterized only by `MNGR_AGENT_NAME`. There is no way for a command to ask the user for a value first, so any workflow that needs free-form input per keypress is impossible from the board.

The motivating case is tagging. `mngr label <agent> -l tag=<value>` plus a `[plugins.kanpan.columns.tag]` column already gives a colored, filterable per-agent tag, and a custom command can already bind a key to one **fixed** tag value. What is missing is typing the value: N tag words costs N keybindings and a kanpan restart, and a tag you did not anticipate cannot be applied at all.

This spec adds one optional field, `prompt`, to `CustomCommand`. When set, pressing the key opens a one-line input on the board; the submitted text is passed to the command as the `MNGR_INPUT` environment variable. Free-form tagging becomes config:

```toml
[plugins.kanpan.commands.t]
name = "tag"
prompt = "tag: "
command = 'mngr label "$MNGR_AGENT_NAME" -l "tag=$MNGR_INPUT"'
refresh_afterwards = true
```

The mechanism is deliberately generic rather than a tagging feature: the same seam serves `mngr rename`, `gh pr comment`, setting `labels.project`, or an ad-hoc `mngr message`.

**Audience:** developers implementing or reviewing the change, in `libs/mngr_kanpan`.

**Related:** [kanpan README](../libs/mngr_kanpan/README.md) (user-facing config reference), `libs/mngr/imbue/mngr/cli/label.py` (the label write this is expected to drive).

## Goals

- A custom command may declare a one-line prompt; the typed text reaches the command as `MNGR_INPUT`.
- The prompt reuses kanpan's existing input affordance, so readline editing works identically to the peek reply.
- Submitting an empty line is a valid submission (it is how a label gets cleared); cancelling is a separate, explicit key.
- A cleared label blanks its cell on the next refresh rather than lingering until a full refresh.
- A slow or hung prompted command cannot stall board refreshes.
- Absent `prompt`, every existing command behaves exactly as it does today.

## Non-goals

- **Prompting for markable (batch) commands.** Rejected at config load in this change; see [Batch prompting](#batch-prompting-follow-up).
- **A built-in tag keybinding.** Kanpan does not pick or bless a label key. The user's choice of key never becomes kanpan's ABI.
- **Grouping the board by a label.** `BoardSection` is a closed enum computed from PR state plus the muted bit; group-by-label is a separate, larger change.
- **Multi-line or multi-field prompts.** One line, one value.
- **Prefilling the input with a current value.** A generic prompt does not know which field (if any) its command reads, so there is nothing to prefill from. The input always opens empty.
- **Fixing `mngr label`'s missing delete path or its offline-host data loss.** Both are real and are described under [Inherited limitations](#inherited-limitations) so the implementer does not mistake them for regressions, but they are separate PRs in `libs/mngr`.

## Configuration

One new field on `CustomCommand` (`libs/mngr_kanpan/imbue/mngr_kanpan/data_types.py`):

```python
prompt: str = Field(
    default="",
    description="When non-empty, pressing the key opens a one-line input using this text as the "
    "caption; the submitted text is passed to the command as the MNGR_INPUT env var.",
)
```

Empty (the default) means no prompt, which doubles as the off switch -- no separate boolean and no validation of the field's contents are needed.

`CustomCommand` is a `FrozenModel` (`extra="forbid"`), and `_load_user_commands` re-validates raw TOML dicts through `CustomCommand(**value)`, so a misspelled key is rejected. Note this surfaces today as an unhandled `ValidationError` escaping `run_kanpan`; improving that is out of scope.

Setting both `prompt` and `markable` is rejected by a `model_validator` on `CustomCommand` (see [Batch prompting](#batch-prompting-follow-up) for why). The validator must **not** `raise ValueError(...)` directly: `check_builtin_exception_raises` is at `snapshot(0)` for this package. Follow the existing precedent in `data_source.py`, which defines a domain error that also inherits the builtin pydantic expects (`OldestCreatedNoInputsError(KanpanDataSourceError, ValueError)`), and raise a named error of that shape.

The existing `command` field's description mentions only `MNGR_AGENT_NAME` and must be updated to mention `MNGR_INPUT`, since field descriptions are user-facing config metadata.

## Behavior

### Opening and editing

Pressing a prompted command's key on a focused agent replaces the footer keybinding belt in place with a bordered one-line input captioned with `prompt`, and moves keyboard focus into it. The board stays visible above. Editing is full readline (Ctrl-A/E/W/K/U, Meta-B/F, and the Option/Ctrl+arrow chords kanpan adds), because the widget is the same `ReadlineEdit` the peek reply uses.

If no agent is focused, the key does nothing and no prompt opens.

The target agent is **captured when the prompt opens**, not read from live focus at submit time. A periodic refresh landing while the user is typing must not retarget the command. If the captured agent has disappeared by the time the command runs, the command fails and reports through the normal footer error path; no special handling is needed.

### Submitting and cancelling

- **Enter** submits, including an empty line. The prompt closes, focus returns to the board, and the command runs in the background exactly as a non-prompted command does today: footer shows `Running <name> on <agent>` with the spinner, then a transient completion or failure message, then a local refresh if `refresh_afterwards` is set.
- **Esc** or **Ctrl-C** cancels. Nothing runs, nothing changes, no message. Ctrl-C must not exit kanpan while the prompt is open.

Empty-submits-are-valid is a deliberate choice, not an oversight: `mngr label X -l "tag="` is the only way to clear a label, so an empty line has to be deliverable. Esc carries the cancel meaning instead.

### Execution

The typed text is added to the command's environment as `MNGR_INPUT` alongside `MNGR_AGENT_NAME`. It is **not** interpolated into the command string. This keeps the value out of the shell's parse phase; the user is still responsible for quoting `"$MNGR_INPUT"` in their command, and the README must show the quotes.

## Implementation

### Key routing

Keys reach the input through **urwid focus**, not through the input handler: `_open_peek` sets `frame.focus_position = "footer"` and the panel's `Pile` focuses the Edit, so the board's `ListBox` is structurally shielded and only keys the Edit refuses (`enter`, `esc`, `ctrl c`) bubble out to `_KanpanInputHandler.__call__`. The prompt follows the same two-layer pattern.

`Enter` bubbles only because the Edit is built with `multiline=False`; `urwid_readline` binds `enter` into its keymap exclusively in the multiline branch.

**The gate's position in `_KanpanInputHandler.__call__` is load-bearing.** A `prompt_open` gate must sit after the help-overlay and peek gates and **before** the `" "` (peek), `q`/`ctrl c` (quit), `U` (unmark all), and command-map branches. Placed lower, a typed space or a letter that happens to be a command key would fire a board action while the user is typing.

### State and widget

- Add one gating field to `_KanpanState` holding the in-flight prompt (the target agent name, the command, and the Edit).
- **Declaration order matters.** `tui.py` has no `from __future__ import annotations`, so pydantic resolves annotations at class-definition time. Any new type referenced by a `_KanpanState` field must be defined **above** `_KanpanState`, or the module fails to import with `NameError`.
- Reuse `_make_reply_edit(caption)` as-is. Its parameter is a `tuple[str, str]` of (palette attribute, text), not a bare string, so the `prompt` string must be paired with a palette attribute. Add a dedicated attribute rather than borrowing `peek_user`, so prompt styling is independent of the peek panel.
- Closing must restore both the saved footer **and** `frame.focus_position = "body"`. Omitting the focus restore leaves the board unable to receive keys, which presents as a frozen TUI.
- The prompt and the peek panel both own the footer slot. They are mutually exclusive by construction (the peek gate precedes command dispatch, so a command cannot start while peek is open), but the reverse must be blocked explicitly: the space/peek branch must not run while a prompt is open.

**A footer re-render cannot destroy the prompt.** `_render_footer` is the sole writer of the `footer_left_text` / `footer_left_attr` widgets; it does not touch the frame's footer slot. So spinner ticks, stamp ticks, and transient messages fire harmlessly while the prompt is installed -- they are simply not visible until it closes. Because submitting closes the prompt *before* the command is launched, the `Running ...` and completion messages are visible as normal. No equivalent of `_on_peek_reply_poll`'s three-way panel-state branch is needed.

### Passing MNGR_INPUT

`_run_shell_command`'s inner `_do_run` and the module-level `_run_shell_command_sync` (batch path) build the same `subprocess.run` invocation twice. **Unify them into a single helper** that takes the agent name and the input text, rather than adding `MNGR_INPUT` to both copies. This keeps the `test_prevent_direct_subprocess` ratchet from growing and removes the duplication a reviewer would flag anyway.

### Executor isolation

Prompted commands must not use the shared `state.executor` (`max_workers=1`), which also serves board refreshes, batch execution, and mute persistence. A prompted command runs on an explicit keypress against a possibly-unresponsive host and is bounded only by the 60-second subprocess timeout, so on the shared executor one hung write stalls refreshes for up to a minute.

Add a dedicated single-worker executor, mirroring `peek_reply_executor`, and shut it down in `run_kanpan`'s `finally` block alongside the existing three. Omitting the shutdown leaks a live thread past exit.

This is not a hypothetical concern. Minh Trinh shipped a foreman button backed by a persisted mngr label (`dd1433a417`, 2026-07-22) and replaced it with local state the same day (`42800a449a`); the replacement's docstring states the reason: "no per-agent host round-trip to set an mngr label (that was slow and could hang on an unresponsive host)". Kanpan's own mute deliberately uses plugin data rather than labels for the same reason. A prompted command degrades better than those cases -- explicit keypress, visible spinner, bounded timeout, footer error -- but the latency is real and must not be shared with the refresh path.

### Clearing a label must blank the cell

`LabelsDataSource.compute` emits no field at all for an empty label value (the `if value:` guard), and `_carry_forward_fields` merges the previous snapshot underneath a local refresh, so a cleared label's cell survives until the next **full** refresh. Since clearing is a first-class use of the prompt, fix it here: emit the field with an empty value so the merge overwrites the stale cell.

Two existing tests assert the current absent-field behavior (`labels_test.py::test_labels_compute_agent_without_label` and `::test_labels_compute_multiple_agents`) and must be updated in the same change.

**Note:** this fixes the *display*. It does not make the label truly absent -- see [Inherited limitations](#inherited-limitations).

### Discoverability

`_build_legend_bindings` and the `?` overlay render `cmd.name` only, so a prompted command is today indistinguishable from a fixed-value one. Append a marker (a trailing ellipsis, matching the convention that a menu item opens further input) to prompted commands in the overlay.

### Refresh caveat to preserve

`_start_local_refresh` early-returns when a refresh is already in flight, and `_on_custom_command_poll` requests one exactly once with no retry. A `refresh_afterwards` repaint can therefore be silently dropped if a refresh happens to be running when the command finishes; the tag then appears at the next periodic refresh. This is pre-existing behavior, not introduced here, and is out of scope -- but do not write a test that assumes the repaint is unconditional.

## Testing

Unit tests (`_test.py`):

- `data_types_test.py`: `prompt` defaults to `""`; a `CustomCommand` with `prompt` round-trips; an unknown key still raises.
- `tui_test.py`: pressing a prompted key opens the prompt and focuses the footer; pressing a non-prompted key still runs immediately; Enter submits and passes `MNGR_INPUT`; Enter on an empty line submits an empty string; Esc cancels without running; Ctrl-C cancels rather than exiting; the prompt gate swallows space/`q`/command keys; closing restores footer and body focus; the prompted executor is distinct from `state.executor`; `prompt` + `markable` is rejected.
- `labels_test.py`: an empty label value now emits a field with an empty value (updating the two tests named above).

Existing tests that break and must be updated:

- `tui_test.py::test_run_shell_command_submits_future` calls `_run_shell_command(state, cmd)` positionally; unifying the subprocess helper changes the signature.
- Any `_load_user_commands` test asserting the exact field set of `CustomCommand`.

Manual verification (per `CLAUDE.md`, do **not** crystallize into pytest): drive the real TUI with `tmux send-keys` / `capture-pane` on a dedicated socket (`tmux -L <name>`, never a bare `tmux` from inside tmux) and confirm the prompt renders, readline chords work, Enter tags, an empty Enter clears the cell, and Esc cancels.

Gates to satisfy: `libs/mngr_kanpan/pyproject.toml` sets `fail_under = 85`, so new production lines need real coverage.

Ratchets to watch, with current snapshots:

- `check_direct_subprocess` at `snapshot(7)`. Unifying the two `subprocess.run` call sites should hold it flat or reduce it; adding a third would raise it. Do not add one.
- `check_builtin_exception_raises` at `snapshot(0)`. See the validator note under [Configuration](#configuration).
- `check_inline_functions` at `snapshot(2)`. The existing inner `_do_run` closure is one of these; folding it into a module-level helper reduces the count.

If a count moves legitimately, re-snapshot with `uv run pytest --inline-snapshot=trim <path>` (scoped, no xdist) and justify the change in the PR description, not in a code comment. Do not restructure code to dodge a regex.

## Inherited limitations

These are properties of `mngr label`, not of this feature. Document them in the README's tagging example; do not attempt to fix them here.

- **No label delete.** `_merge_labels` is `{**current, **new}`; there is no `unlabel` command or `--remove` flag. An empty submission sets the empty string, so the cell blanks but `has(labels.tag)` stays true forever and presence filters keep matching. The real fix is a `--remove KEY` flag on `mngr label`.
- **Labels written while a host is offline are lost on restart.** The offline path writes only the provider-side mirror; nothing copies it back into the host's `data.json`. Kanpan lists offline agents, so a prompt on one will appear to succeed and then quietly lose the value.
- **`mngr c <name> --reuse --update` replaces the label set wholesale**, dropping labels not re-supplied.
- **Label keys must be bare CEL identifiers.** A key containing a dash compiles as subtraction in `--label` and silently matches zero agents. Recommend `tag`.
- **`[plugins.kanpan.columns.*]` swallows typos.** It is read with bare `.get()` calls under `model_construct`, unlike `shell_commands` which uses an `extra="forbid"` model. A misspelled `colors` vanishes with no error, and an invalid urwid color name is a latent `AttrSpecError` crash that only fires once some agent carries that value.

## Batch prompting (follow-up)

`prompt` combined with `markable` cannot work as written: `_dispatch_command` tests `_mark_color(cmd) is not None` and returns via `_toggle_mark` **before** any execution branch, so a markable command never reaches a prompt. This change rejects the combination at config load with a clear message.

The useful behavior -- mark N agents, press `x`, answer one prompt, apply the same value to all -- belongs in a follow-up: prompt once in `_execute_marks` before `_start_batch_execution` builds its work items, and thread the collected text through each item. Note that `_finish_batch_execution` already calls `_start_local_refresh` unconditionally, so `refresh_afterwards` is a no-op on the batch path today.

## Documentation

Update `libs/mngr_kanpan/README.md`:

- Document `prompt` and `MNGR_INPUT` in the "Custom commands" section, alongside the existing `markable` / `refresh_afterwards` fields, including that `prompt` + `markable` is rejected.
- Show `"$MNGR_INPUT"` **quoted** in every example. The value is passed via the environment rather than interpolated into the command string, but the command still runs under `shell=True`, so an unquoted expansion is word-split and glob-expanded.
- Add a worked "tag agents from the board" example combining a prompted command with a `[plugins.kanpan.columns.tag]` column, and note the label caveats from [Inherited limitations](#inherited-limitations) that a user will actually hit -- that an empty value clears the display but leaves the key present, and that a label key should be a bare identifier such as `tag`.
- Fix the existing inaccuracy in the "Refresh behavior" section, which says the agent-only refresh "runs only local data sources (repo_paths, git_info)". Label-backed columns are local too (`is_remote` is `False`) and do run on that path -- which is exactly why `refresh_afterwards` repaints a tag.

## Changelog

One entry at `libs/mngr_kanpan/changelog/<branch-name>.md` (slashes replaced with dashes), describing the new `prompt` field, `MNGR_INPUT`, and the cleared-label display fix. No entry is owed to `libs/mngr` unless a file there is touched.
