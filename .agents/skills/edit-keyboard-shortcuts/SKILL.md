---
name: edit-keyboard-shortcuts
description: Use when the user asks you to add, change, remove, reset, or list a keyboard shortcut of the workspace (e.g. "make Cmd+J open the launcher", "add F6 for next window", "what's the shortcut to stop the agent?", "put the overview back on its default key").
metadata:
  author: imbue
---

# Editing the workspace's keyboard shortcuts

Every built-in app declares **actions** (things a key can do: open the launcher,
stop the agent, rename a file) with default keys. The user can change which keys
run each action; the Keyboard shortcuts screen (Workspace settings in Imbue
Studio, or right-click the desktop) is the same thing with buttons. You change
them through the shell's HTTP routes on loopback. The change applies at once in
every open window; nothing needs reloading.

You can only bind keys to actions an app already declares. You cannot invent a
new action here (that is an app change: the app's `[[keybindings]]` in its
`app.toml` plus the handler). Imbue Studio's own shortcuts (New window, Quit,
zoom, ...) are not in this catalog; for those, send the user to Imbue Studio's
Workspace settings > Keyboard shortcuts.

```bash
SHELL_URL="${MINDS_WORKSPACE_SERVER_URL:-http://127.0.0.1:8000}"
```

## 1. Read the catalog first, every time

```bash
curl -sf "$SHELL_URL/api/keybindings/catalog" | jq '.apps[] | {app, display_name, actions: [.actions[] | {id, label, chords, default_chords, scope, source}]}'
```

- `app` + `id` name an action; `label` and `display_name` are what the user
  calls it. Match the user's words against `label`, never guess an id.
- `chords` is what runs the action now, `default_chords` the app's default.
- `scope`: `workspace` actions run anywhere in the workspace; `app` actions only
  in their own app's window.
- `source`: `default`, `workspace` (an override saved here), `account`, or
  `unsent`.

## 2. Chord spelling

Lowercase tokens joined with `+`: modifiers in the order `ctrl`, `alt`, `shift`,
`meta`, then one key. The key is a single character (`k`, `/`, `.`, `]`) or one
of `enter escape tab space backspace delete insert home end pageup pagedown
arrowup arrowdown arrowleft arrowright plus f1`...`f12`.

`meta` is the primary modifier: Command on a Mac, Ctrl on Windows and Linux. So
"Cmd+Shift+J" is `shift+meta+j`, and "Option-Command-O" is `alt+meta+o`. When
the user says "Ctrl" on a Mac, they mean `ctrl`; on other platforms they almost
certainly mean `meta`. If you cannot tell, ask.

Refused: `alt+meta+delete` (Ctrl+Alt+Del). A `workspace`-scope action needs a
modifier other than Shift, or a function key; the route refuses anything else.

## 3. Check for clashes before saving

The route does not check clashes; you must. A new chord clashes with any action
that already holds it among:

- the other actions of the same app;
- for a `workspace`-scope action, every action of every app;
- for an `app`-scope action, the `workspace`-scope actions of every other app.

If it clashes, tell the user which action has the key now (by its label and app
name) and ask whether to take it from that action. Only on a yes, save the other
action first with that chord removed from its list (an empty list unbinds it),
then save the new one.

## 4. Save

A save replaces the action's whole chord list. To **add** a key next to the
existing ones, send the current `chords` plus the new one; to **replace**, send
only the new one.

```bash
curl -sf -X POST "$SHELL_URL/api/keybindings" -H 'Content-Type: application/json' \
  -d '{"app": "system_interface", "action": "open-launcher", "chords": ["meta+k", "meta+j"], "scope": "workspace"}'
```

`scope` here is where the save applies, not the action's scope:

- `"workspace"` (use this unless asked otherwise): this workspace only.
- `"account"`: every workspace of the user's account. This may show the user a
  permission prompt the first time; the answer's `delivery.status` says whether
  it reached the account. Use it only when the user asks for "everywhere" or
  "all my workspaces".

A 400 answer's `detail` says what was wrong (bad spelling, refused chord,
missing modifier, unknown action); fix and retry, or tell the user.

## 5. Reset to the default

```bash
curl -sf -X POST "$SHELL_URL/api/keybindings/reset" -H 'Content-Type: application/json' \
  -d '{"app": "system_interface", "action": "open-launcher"}'
```

This drops this workspace's override, so the account's choice (or the default)
answers again.

## 6. Confirm and tell the user

Read the catalog again and check the action's `chords` are what you meant. Tell
the user in their own terms: the action's label, the keys written the way their
platform shows them (⌘ ⌥ ⇧ ⌃ on a Mac), and any action that lost a key. Do not
mention routes, ids, or JSON.
