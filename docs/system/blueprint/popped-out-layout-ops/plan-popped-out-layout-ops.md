# Layout ops on popped-out windows

## Overview

- A window the user has popped out of the desktop into its own Imbue Studio window (mngr `specs/pull-out-window/spec.md`) is the user's deliberate arrangement. Agent layout ops must not quietly undo it: an op that would change a popped-out window's placement is refused unless the agent passes `--force`.
- Ops that don't change the pop-out keep working, and three of them start working properly on it: `refresh <window>` reaches the pop-out's page, `focus` and `open` raise the pop-out instead of pulling the window back, and `reload_system_interface` leaves the pop-out a pop-out.
- The pop-out's shell registers its connection with the shell server under its client's id, so targeted ops reach it. A registration from a pop-out never reports an active desktop, so the client's active desktop stays its main window's.
- A pop-out reopened at launch trusts the stored layout: if its window was brought back while the client was away, it closes, as the pull-out spec's section 4.5 already says. The grace that marks a window popped out again stays for a freshly torn-out pop-out only.
- The work ships as three PRs. On template main: the three pop-out bug fixes (`refresh <window>` delivery, `?solo=` surviving a reload, the reopened pop-out closing). On template main after #759 (`workspace-layout`): the op rules, the agent-facing docs, and this plan. On mngr: the reopened-at-launch signal and the pull-out spec fixes.

## Expected behavior

### Which ops a popped-out window blocks

- "Popped out" is judged per client, from the target client's own placement of the window, and only while that client is connected.
- A client counts as connected when any of its app windows holds the socket, a pop-out included. A client whose only open window is a pop-out is connected: its pop-outs block ops, and ops can target it.
- Refused without `--force`: `restore`, `maximize`, `place`, `minimize`, and `open --beside <window>` naming a popped-out partner.
- A refused op changes nothing, including the desktop switch `--desktop` would otherwise make. It exits with code 4 and says the window is popped out and that `--force` overrides it.
- Every mutating verb accepts `--force`. It is ignored where nothing can be refused.
- With `--force`, `restore`, `maximize` and `place` do what they do today: the window comes back onto the desktop, and its pop-out closes.
- With `--force`, `minimize` brings the window back onto the desktop minimized, so it actually goes out of sight. Today it only hides the ghost, leaving the pop-out on screen.
- A forced op brings the window back onto its own desktop. The main window does not switch to that desktop.
- A forced op that brings a window back adds a short note to its summary, e.g. "(brought back from its own window)".
- Never refused: `refresh` (either form), `show`, `navigate`, `load`, `close`, the shortcut and wallpaper ops, and the read-only `context`, `desktops` and `list`.

### `close`

- `close` never checks pop-outs. It closes the window for everyone, as it does today, and that ends every client's pop-out of it.
- Its summary carries no note about the pop-outs it ended.
- The critical-app flow closes `<name>-preview` each round as it does today, popped out or not. The reopened preview lands on the desktop.

### Raising instead of pulling back

- `focus` on a popped-out window raises its pop-out. The window stays popped out, and the main window keeps its desktop, as `show` already does.
- `open` whose `--if-present focus` finds a popped-out window at the path raises the pop-out the same way. This covers the common `open chat --path "/?chat=<id>"` in `AGENTS.md`, `caretaker`, `manage-scheduled-tasks`, the scheduled-agent prompt and the update-self chat reopen.
- A popped-out window whose ghost the user hid stays that way: the pop-out is raised, and the ghost stays hidden.
- The summary of a `focus` or `open` that raised a pop-out says the window was raised in its own window.
- The pop-out raises itself when a `show` or `focus` names its own window, so a raise works even when the user has closed the main window.

### `--beside`

- `open <app> --beside` whose partner is popped out opens the new window where a plain `open` puts it (cascade position, on top), without pairing, and prints a note saying why. This applies to a bare `--beside` (the agent's own chat) and to a named partner.
- `open --beside --force` brings the partner back onto the desktop and pairs the two as today.

### Content ops on a pop-out

- `navigate` moves the pop-out's page like any window's page: for everyone on a linked window, for the target client alone on an independent one.
- `show` keeps its behavior: it raises a popped-out window it lands on, and may move it to its path.
- `refresh <window>` reloads the pop-out's page, and also the main window's hidden copy of it.
- `refresh --app` reloads every page of the app, pop-outs included, as it does today.
- The main window keeps its hidden, loaded copy of a popped-out window's page, so dropping the window back onto the desktop is instant.

### Reloads and relaunch

- `reload_system_interface` reloads a pop-out as a pop-out. `refresh_workspace_view.py` no longer turns a pop-out into a full desktop for a moment.
- A pop-out reopened at launch whose window was brought back while its client was away closes itself, once its first layout says so.
- A freshly torn-out pop-out whose first layout doesn't yet say the window is out still waits the grace for the desktop's save, then marks the window popped out itself.
- When Minds doesn't say whether a pop-out was reopened at launch (an older Imbue Studio), the shell treats it as freshly torn out, which is today's behavior.
- An op applied while the client was disconnected (a pop-out doesn't block then) and a pop-out reopened at the next launch agree: the pop-out closes.

### What agents can see

- `desktops` and `list` list each client's popped-out windows, and whether each one's ghost is hidden. They do so for every client, connected or not, next to the existing `is_connected` and `shown`.
- `context` lists no windows and is unchanged.
- An op that surfaces a desktop window (`open`, `focus`, `show`) for a client whose only open window is a pop-out succeeds and is stored for when the main window reopens. Its summary notes that the client has no desktop window open.
- `manage-desktop` explains popped-out windows: how to spot one in `desktops`, which ops refuse and why, `--force`, and the ways a user brings a window back (drag the pop-out onto the Mind window, the button in its bar, "Bring back to desktop" on the ghost or its taskbar entry).
- The agent guidance for a refusal: pass `--force` when the user asked for exactly that change, otherwise tell the user the window is popped out and how to bring it back.

### Out of scope

- Agents popping windows out or bringing them back themselves.
- An Imbue Studio Playwright spec that tears out a window in real Electron (a follow-up).

## Changes

### Template PR on main: pop-out bug fixes

- The pop-out's shell registers its websocket connection with the server under the client's id, marked as a pop-out's.
- The server keeps a pop-out's registration for targeting ops, and it counts toward the client being connected. It never records it as the client's report, never logs a desktop switch for it, and never lets it supply an active desktop to `context` or anything else.
- The pop-out's shell keeps `?solo=<window>` in its own URL, so any reload of its page comes back as the pop-out.
- The pop-out's shell learns from its URL whether Imbue Studio reopened it at launch. A reopened pop-out whose first layout says the window is back reports that at once, so Imbue Studio closes it. Only a freshly torn-out one runs the grace heal.
- Tests: store and page-layer unit tests for the registration and the reopened-at-launch rule. New pop-out cases in `test_e2e.py`, where a second page in the same browser context opened at `/?solo=<window>` is a real pop-out-mode shell: `refresh <window>` reloads it, `reload_system_interface` leaves it in pop-out mode, and a reopened pop-out closes when its window is back.
- A changelog entry for `system_interface`.

### Template PR after #759: layout-op rules

- The shell's op route checks the target client's placement of the target window before applying `restore`, `maximize`, `place`, `minimize` and `open --beside`. A popped-out window refuses the op, with a refusal distinct from the existing 409 and 503, unless the op carries `force`.
- A refused op applies nothing, the `--desktop` switch included.
- `focus`, and an `open` that finds an existing window, send the client the `show` layout op for a popped-out window instead of raising its placement.
- `open --beside` with a popped-out partner places the new window as a plain `open` does unless forced.
- `minimize` with `force` brings the window back onto the desktop minimized.
- A forced op that brings a window back does not switch the client's active desktop.
- The op answers carry what the new summaries need: raised in its own window, brought back, not paired, and no desktop window open. The last one comes from the server's registrations: the client has only pop-out connections.
- The inventory document and the `desktops`/`list` output list each client's popped-out windows with their ghost-hidden state.
- The `workspace_layout` library gains the refusal as a typed error and the new answer fields. Its `workspace-layout` CLI gains `--force` on every mutating verb, exit code 4, and the new summary lines.
- The pop-out's shell handles `show` for its own window by asking Imbue Studio to raise its window. Imbue Studio already raises an existing pop-out on `POP_OUT_WINDOW`; confirm it accepts that from the pop-out's own page.
- `manage-desktop` documents popped-out windows as listed under "What agents can see". `build-app`'s `--beside` step mentions the unpaired case.
- `desktop-interface/contracts.md` section 8 (the op contract) and the placement notes describe the refusal, `force`, the raise, and the pop-out's registration.
- This plan lands in this PR.
- Tests: op-route tests for every refused, forced and raised case, and for a refused op leaving files and the active desktop untouched. `workspace-layout` CLI tests for `--force`, exit code 4 and the summaries. `test_e2e.py` cases for `focus` and `open` raising a pop-out-mode page, and for a forced op bringing the window back.
- Changelog entries for `system_interface`, `workspace_layout` and the skills.

### mngr PR: Imbue Studio signal and pull-out spec

- Imbue Studio marks a pop-out it reopens at launch (session restore, dock reopen, backend retry) so its shell can tell it from a freshly torn-out one, e.g. a parameter next to `solo` in the pop-out's workspace URL.
- `specs/pull-out-window/spec.md`: fix section 7.5's first-load rule to match section 4.5 (a reopened pop-out whose window is back closes), and cross-reference this plan for layout ops on popped-out windows.
- Minds unit tests: session restore and dock reopen mark the pop-out as reopened; a tear-out and the window menu's "Open in its own window" don't.
- A changelog entry for `minds`.

### Verification

- A manual pass in real Imbue Studio before merging each template PR: tear out a window, run each op against it with and without `--force`, `refresh <window>`, `refresh_workspace_view.py`, and quit and relaunch after a forced op.
