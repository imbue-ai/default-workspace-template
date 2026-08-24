# What stops a message, and what the user sees: the whole picture

Four things now decide what happens when a message does not reach an agent. Three exist; one is proposed.
They were built at different times for different reasons, and the point of this document is to say plainly where each one applies, so the next change lands in the right layer instead of a fourth one.

Read this before touching the send path.

## The layers, in the order a message meets them

### 1. Composer guards -- the harness says "not from here"

**Where:** `MessageInput.ts`, driven by `HarnessSpec.popups` shipped over `/api/harnesses`.
**When:** before anything is sent, on the text the user typed.

A harness declares which slash commands the chat refuses, and why. A `composer_command` popup matches the first token of the message; a match opens a notice and the message is never sent. `PopupAction.OPEN_AUTH` routes `/login` to the auth surface; `PopupAction.NOTICE` explains the refusal.

**Recently gained a custom body.** `HarnessPopup.notice_body` lets a harness say *why* a specific command is declined, replacing the default "You can still send it from the agent's terminal". The composer stays free of per-command branching -- which is the whole reason these are declared server-side.

This layer is per-harness and pre-send. It cannot know anything about the agent's current state, because nothing has been looked at yet.

**Gap worth naming:** claude declines `/config`, `/diff`, `/cost` and friends, but *not* `/model`, `/fast`, `/effort` -- those are the model bar's own typed commands and must get through. Typed by hand from chat they reach the TUI, open a picker, and the display rules hide the message from the transcript, so the user sees a picker appear for no visible reason.

### 2. Send preflight -- mngr says "something already owns the input"

**Where:** `mngr_claude/dialogs.py` and `_preflight_send_message`, inside mngr.
**When:** at send time, before pasting, by reading the agent's pane.

This is the dialog registry. `classify` reads a `capture-pane` snapshot and returns what is holding the input, then `deal_with_dialogs` acts on it, looping because dismissing one surface can reveal another.

Three kinds, told apart by what answering one costs:

| kind | example | what mngr does |
| --- | --- | --- |
| benign, recognised | the theme / model / effort pickers, the settings window | Escape it; the conversation is untouched |
| non-benign, recognised | model-switch and effort-switch confirmations, usage limit, LSP install | answer it, but only on the option named for it, and only if the operator opted in |
| unrecognised | anything else holding the input | refuse the send, with an actionable error |

Shell mode rides the same loop: an empty `!` strand self-clears with Backspace, a human's unsubmitted `!<command>` refuses.

**The operator's consent is two named tokens**, not a wildcard. `ALL_RECOGNIZED_NONBENIGN` covers everything mngr can name, each on the option named for it, so the set grows with the catalogue without ever becoming a guess. `DANGEROUS_ALL_NONBENIGN_INCLUDING_UNRECOGNIZED` additionally covers surfaces nobody has looked at -- guessing -- and says so in its name.

**What makes any of this work is positional.** The input box is identified by being at the *bottom* of the pane, not by the `❯` glyph, because Claude renders every past user turn with that same glyph at column 0. A whole-pane search reports "the box is here" for any conversation with history, however the pane is really occupied -- which is why dialog detection silently did nothing before this was fixed.

This layer knows the pane and nothing about the UI. It reports in words meant for a human.

### 3. The failure notice -- the chat says what went wrong

**Where:** `MessageInput.ts`, fed by the send endpoint.
**When:** after a send has failed, whatever the reason.

The reason now travels: `send_to_agent` returns the harness's own words rather than a bool, and the chat path raises them as `SendFailedError` so the endpoint answers with that text instead of "0 successful agents". The chat shows it in the workspace's own notice rather than a native alert.

**Deliberately not a harness popup**, though it looks like one. The `composer_command` and `turn_check` triggers both fire in the frontend, before a send, off what the user typed. A send failure is the backend reporting, after the fact, that something refused -- and every harness can fail a send, so declaring it per harness would be wrong.

### 4. Recovery actions -- built

Cancel returns the message to the composer, Retry repeats the operation, Force restarts the agent and sends. Specified in `send-failure-notice.md`.

**One notice, one way in.** `NoticeDialog` owns the overlay, the Escape listener, the backdrop press and the focus rule, so every notice in the chat dismisses identically -- previously each was hand-copied and they had already drifted, with one of them not answering Escape at all. A view that is not the composer raises a failure through `raiseFailureNotice`, so the queued-message flush reports into the same notice rather than a system alert. Nothing in the send path calls `alert` any more.

The load-bearing decision: **actions attach to the operation, not the failure.** Any send can fail, and in every case the same three ways out apply, so there is no per-failure branching -- only the explanation varies, and that already arrives from layer 2. A failed *interrupt*, which shares this notice, is not repeatable and keeps a single OK.

## How they fit

Each layer answers a different question, and none can answer another's:

- **1** knows what the user typed. It cannot know the agent's state.
- **2** knows the pane. It cannot know what the UI should show.
- **3** knows how to tell the user. It cannot know why -- it repeats what 2 said.
- **4** knows what to do about it. It needs 3's text and 2's honesty about what failed.

The chain only works because layer 2 stopped flattening its reason into a bool. Before that, layer 3 could only ever say "0 successful agents", and layer 4 would have been a prettier box around nothing.

**Where a new change belongs:**

- "this command should not be sent from chat" -> layer 1, as a declared popup, with a `notice_body`.
- "mngr should handle this surface" -> layer 2, as a catalogue entry, benign or with a named option.
- "the user should be told differently" -> layer 3.
- "the user should be able to do something about it" -> layer 4.

## The two known rough edges

**A stray Enter can answer a dialog nobody looked at.** `submit_message_and_confirm` re-sends Enter while it cannot confirm delivery, gated on the pane still showing the pasted message. That gate is a substring test: `/effort` normalizes to `effort`, and the picker it opens is headed **Effort**, so the gate reads the open picker as an unsent message and presses Enter at it -- confirming whatever row was highlighted. `/model` has the same shape. The gate's own comment states the assumption that fails: a stray Enter on an empty input row is a no-op, but on a selector it picks a row.

This is the exact thing layer 2 exists to prevent, happening one layer down, and it predates all of this.

**An attachment that fails to upload now refuses the send** and names the file, rather than being dropped from the message with its chip cleared a moment later.

**Bare `/effort` and `/model` are invisible.** Layer 1 lets them through by design, the display rules hide them, and layer 2 will Escape the picker at the next send -- so the user's action produces a picker that appears, does nothing, and vanishes.

## Where each piece lives

| piece | file |
| --- | --- |
| declared popups, `notice_body` | `imbue/system_interface/harnesses/registry.py` |
| composer guard, notices | `frontend/src/views/MessageInput.ts` |
| shared backdrop dismissal | `frontend/src/views/modalBackdrop.ts` |
| dialog catalogue, `classify` | `mngr_claude/imbue/mngr_claude/dialogs.py` |
| preflight, consent tokens | `mngr_claude/imbue/mngr_claude/plugin.py` |
| the reason's route to the UI | `imbue/system_interface/agent_discovery.py`, `server.py` |
| Enter retry and its gate | `mngr/imbue/mngr/agents/tui_utils.py` |
