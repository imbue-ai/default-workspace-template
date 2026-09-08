# Deciding what owns Claude Code's input before a send

## Overview

- `send_message` already ran `_preflight_send_message` before `wait_for_tui_ready` (`libs/mngr/imbue/mngr/agents/tui_agent.py`), so the intended order -- resolve whatever is holding the pane, *then* check readiness -- was in place.
  The defect was not the order. It was that every one of those checks decided "who owns the input" from a signal that does not mean what it was documented to mean.
- That signal is `INPUT_PROMPT_LINE_RE`, `^❯` (`libs/mngr_claude/imbue/mngr_claude/dialogs.py`).
  `has_input_prompt_line` was documented as: "Its ABSENCE is the universal 'something owns the input' signal."
  That is false. Claude Code renders **every past user turn** with `❯` at column 0, so the regex matches the transcript, not the input box.
- Measured on a live agent (workspace `aww`, `minds-staging-Chat-1:0`) while its settings window was open:

  ```
  has_input_prompt_line: True
  classify -> None
  matching lines: '❯ /theme'  '❯ alr'  '❯ put on a very long tool call show for me'
  ```

  Every match is a historical turn. The live input box was not on screen -- the settings window had replaced it.
  `StatusWindow` and `GenericBenign` both *matched* that pane; only the early return in `classify` suppressed them.
- Consequence: in any conversation with at least one prior user message, `classify` returned `None`, preflight cleared nothing, `wait_for_tui_ready` passed instantly, and the send proceeded into a pane mngr had mis-read. Two failure modes followed, both observed:
  - **Swallowed.** The paste lands in the surface that owns the pane. Reproduced directly: with `/config` open, pasting `hello can you hear me` produced `⌕ hello can you hear me` / `No settings match "hello can you hear me"`, and Enter did nothing. For a slash command this is silent, because `send_message` applies the `RELAXED` confirmation policy and logs a warning instead of failing.
  - **Appended, then unconfirmed.** `_detect_preexisting_input_text` finds the last `❯` line -- a transcript echo, not the input box -- and reports it as leftover text. The send appends to it rather than stopping. No evidence probe ever confirms, and 90s later the send errors. From the live workspace's log:

    ```
    WARNING  _warn_if_preexisting_input_text: Input box of agent Chat-1 already contains
             text before sending; the new message will be appended
    ERROR    raise_for_unconfirmed_submission: no evidence probe confirmed the submission within 90s
    ```

    The same log shows the production readiness check as `Waiting for TUI to be ready (looking for: ^❯)`, with no preflight line between it and the send -- preflight found nothing to do.

## Root cause

One wrong assumption, relied on in five places:

| Location | How it used `^❯` | Effect of the false positive |
| --- | --- | --- |
| `classify` | Early return `None` when a prompt line is found | No dialog is ever classified; preflight is a no-op |
| `is_stranded_in_empty_shell_mode` | Returns `False` when a prompt line is found | Empty shell mode is never self-healed |
| `is_pending_shell_command` | Same, as the complement | A human's unsubmitted command is never reported |
| `TUI_READY_INDICATOR` | Was `INPUT_PROMPT_LINE_RE` itself | Readiness passes immediately, whatever the pane shows |
| `_detect_preexisting_input_text` | Scans up from the bottom for the last `❯` line | Reports a past turn as leftover input, appends to it, never confirms |

The assumption is not rescued by requiring column 0: transcript echoes are themselves at column 0.
It is rescued by **position within the pane**.
Claude draws the input area -- or the surface that replaces it -- at the bottom of the pane.
Position is the discriminator, and the only one available from a `capture-pane` snapshot.

## Design: the input region

Every "is the input box present" question is asked of the bottom of the pane.

- `get_input_region(pane_content)` -- the last `INPUT_REGION_LINES` (12) non-blank lines.
  Blank lines are dropped first, so the region is unaffected by padding above a surface's content.
- `get_benign_footer_region(pane_content)` -- the last `BENIGN_FOOTER_REGION_LINES` (3) non-blank lines, used only by the footer-only catch-all.

Two regions rather than one because they answer different questions.
The input region must be generous enough to contain the box plus its chrome (rule / input row / rule / hint) and a wrapping statusLine.
The footer region must be tight, because it is the only thing standing between a transcript that quotes "esc to cancel" and a real Escape keypress: against the real chrome the last three non-blank lines are exactly that chrome, and transcript content starts at the fourth.

A boundary derived from Claude's horizontal rule lines was tried first and **rejected on measurement**: on a ready pane the input box sits *between* two rules, so "after the last rule" excludes the box itself and a ready pane would classify as `Unrecognized`.

Measured against the two real panes that motivate this spec:

| Pane | whole-pane `^❯` | `get_input_region` `^❯` |
| --- | --- | --- |
| Settings window open (workspace `aww`) | `True` (wrong -- transcript echoes) | `False` (correct) |
| Idle, ready for input | `True` | `True` (correct) |

**Note:** the regions are derived from pane text, not pane height.
`capture-pane` returns the window buffer, which can be taller than the attached client can display, so counting lines from the end keeps this independent of client geometry.

## Expected behavior

- Preflight classifies **dialogs and shell mode first**, on their own evidence, with no prior input-prompt or readiness check.
  Only after nothing matches does it ask whether the input box is present, and it asks that of the input region.
- A dialog is detected whether or not the transcript above it contains `❯` lines.
- Shell mode -- empty (self-healed with Backspace) and pending (refused) -- is detected under the same conditions, by the same loop.
- After preflight reports the pane clear, `wait_for_tui_ready` performs a readiness check that can actually fail.
- A turn whose *output* quotes dialog text is never classified as a dialog.
- `Unrecognized` is returned only when the input region shows neither the input box nor anything recognized.

## Why the catalogue is walked first

This ordering is load-bearing, not stylistic, and the smaller change -- keep the prompt check first, merely scope it to the region -- was rejected because it fails on a real capture.

`plugin_test.py`'s `_MODEL_SWITCH_PANE` is the `/model` switch confirmation: about seven non-blank lines.
The echo of the user's own `/model` turn therefore sits *inside* the input region, directly above the dialog.
Asking about the input box first reads that echo as a live prompt, returns `None`, and misses the confirmation this feature exists to catch.
A dialog does not have to be taller than the region, so the region cannot be the first question.

## Implementation

### `libs/mngr_claude/imbue/mngr_claude/dialogs.py`

- Added `get_input_region` / `INPUT_REGION_LINES` and `get_benign_footer_region` / `BENIGN_FOOTER_REGION_LINES`.
- `has_input_prompt_line` searches the input region; its docstring no longer makes the "universal signal" claim that was the defect.
- `classify` walks `DIALOGS` first, then asks about the input box, then returns `Unrecognized`.
- `GenericBenign.matches` requires its footer within the footer region.
- `get_input_row` returns the pane's input row -- the lowest line in the input region starting with the prompt glyph or, in shell mode, `!`.
- `is_stranded_in_empty_shell_mode` and `is_pending_shell_command` test the shell footer against the input region and then key off that input row.
  Asking instead whether a prompt line exists anywhere in the region is not enough, and this is the subtle case: in a short conversation the echo of a past turn falls inside the region, so "a prompt is nearby" is true while shell mode holds the input, and the send walks into it exactly as before -- the same defect, reachable with a shorter conversation.
  A whole-pane footer test is also wrong: a conversation that merely discusses shell mode contains that text, so it would claim shell mode for a pane holding a settings window and refuse the send naming a command that does not exist.
- `DIALOGS` puts the two shell-mode entries first, matching the precedence of the flow they came from, where a pending shell command was checked ahead of any dialog handling.
  It is also the strongest evidence in the catalogue: the shell footer text appears on no ordinary pane (verified against a live agent, idle and with `/config` open), whereas the captions below it are matched wherever they occur.
- `StatusWindow`'s pattern uses `[ \t]+` rather than `\s+`.
  `\s` matches a newline, so five bare words on consecutive transcript lines matched -- and the class is self-clearing, so that cost a real Escape on a live turn.
  The old comment claimed the absence of `DOTALL` prevented this; it does not, since the pattern contains no `.`.

### `libs/mngr_claude/imbue/mngr_claude/plugin.py`

- `get_tui_ready_indicator` returns the `has_input_prompt_line` predicate.
  It is returned from a method and **not** stored in the `TUI_READY_INDICATOR` ClassVar: a plain function on a class is a descriptor, so reading it through an instance binds it, and the readiness poll would call it with the agent as a first argument and raise `TypeError` on every send.
- `_detect_preexisting_input_text` scans the input region rather than the whole pane.
- `ShellCommandPendingError` is deleted, along with the branch that raised it.
  Nothing caught it; it was a `SendMessageError` subclass whose only job was to carry a message that `PendingShellCommand.get_message()` already carries. There is now one error for "something is holding the input": `DialogDetectedError`.
- `DialogDetectedError` takes the dialog's nickname and its message as separate arguments.
  Its sentence interpolates a short description, so passing a whole sentence read as one message wedged inside another. The `mngr connect` line is appended either way: a dialog's own wording has to suit every client -- a Minds user opens a terminal tab and has no CLI -- so it names no command, while this error is raised on the CLI path, where naming one is the difference between advice and an instruction.
  The result is that a pending shell command gives the same recovery it gave before: what state the agent is in, which keys resolve it, and the command that gets you there.

### `libs/mngr/imbue/mngr/agents/tui_agent.py`, `tui_utils.py`

- The indicator type widens to `str | re.Pattern[str] | Callable[[str], bool]` on the ClassVar, on `get_tui_ready_indicator`, and in `wait_for_tui_ready`; `_pane_matches` dispatches on it. The two existing forms keep their exact meaning.
- The readiness log renders a predicate by name, so `Waiting for TUI to be ready (looking for: ...)` stays readable.
- The readiness timeout no longer puts the pane into the raised `SendMessageError`. It is still logged. `dialogs.py` already states this policy: a user-facing message never quotes the pane, which is unbounded and can contain the user's own code or a diff.

### Ordering restored from PR #397

The shell-mode entries lead the catalogue, as they did in the flow PR #397 established, where the pending-command check ran ahead of any dialog handling.
Their evidence is also the strongest available: the shell footer text appears on no ordinary pane (verified against a live agent, idle and with `/config` open), whereas the captions below them are matched wherever they occur.

### Unchanged

`send_message`'s ordering, `deal_with_dialogs`'s loop and no-progress guard, the dialog catalogue, and `sensibly_deal_with_dialogs`.

## Edge cases and failure modes

- **A dialog whose footer is cut off.** The footer catch-all reads the bottom of the pane, so a window sized taller than the client can display hides exactly the lines it depends on. The status-line fix in `sigwinch_panes.sh` is what keeps them on screen; this depends on it.
- **A transcript quoting dialog text.** Handled by the footer region. A quote sits above the bottom chrome.
- **A named dialog whose caption is quoted in a transcript.** Possible: the caption match is whole-pane. For the `Answerable` entries the cost is a refusal, not a keypress -- `cycle_to_option` finds no option and the send raises. Note this is a refusal for as long as that text is on screen, not a stray Escape.
- **`Unrecognized` on a novel surface.** The send refuses with an actionable error rather than pasting into it, but only after a short poll for the input box to appear.
  The poll runs for the same window the readiness check itself uses (30s), because before this branch an unreadable pane was not preflight's business at all -- it fell through to that check, which polls for the prompt for exactly that long. Anything shorter refuses a pane that used to be waited for, which on a slow or cold-starting host is a pane that would have come up fine.
  The poll is not defensive padding, it is required: `create` delivers a new agent's first message as soon as the `session_started` hook fires, and that hook runs when claude STARTS, not when its TUI has painted. Preflight runs before the readiness wait, so refusing on the first look fails the opening message of every new agent -- the `/welcome` chat among them. `wait_for_ready_signal` already declines to treat `Unrecognized` as blocked for the same reason.
  A pane that stays unreadable is still refused.
- **A long stranded message.** Claude puts `❯` on the first row of a wrapped composer, so a message long enough to wrap past the input region is no longer reported as leftover text; the send refuses as `Unrecognized` instead of appending to it.

## Behavior that is new because it now works

Dialog detection and both shell-mode predicates were dead in any conversation with history. They now fire, so these errors appear where they previously did not:

| Error | When |
| --- | --- |
| `DialogDetectedError` naming a dialog | `/config`, `/model`, `/theme` open at send time -- replaces a silent swallow |
| `DialogDetectedError` for pending shell mode | A human left `!<command>` unsubmitted |
| `DialogDetectedError` for `Unrecognized` | A surface mngr cannot name, including the Ctrl+O expanded transcript |
| `SendMessageError` readiness timeout | Readiness genuinely fails -- previously unreachable for claude |

## Testing

- `dialogs_test.py`: a pane with prior `❯` turns above a settings window classifies as `StatusWindow` (the measured failure); a seven-line `/model` confirmation with a `❯ /model` echo inside the region classifies as `ModelSwitchWarning` (why the catalogue is walked first); five tab words on separate lines classify as `None`; a transcript mentioning `! for shell mode` under a settings window classifies as `StatusWindow`; a quoted footer with real bottom chrome classifies as `None`.
- `plugin_test.py`: the readiness indicator is a predicate, accepts a column-0 prompt, rejects an indented selector option, and rejects a past turn echoed above a dialog.
- Manual (tmux, not crystallized per the repo's TUI convention): open `/config` in a live agent with prior turns, send a message, confirm the window is dismissed and the message reaches the model.

## Out of scope

No change to `sensibly_deal_with_dialogs`, the dialog catalogue's membership, or the `Answerable` gating.
