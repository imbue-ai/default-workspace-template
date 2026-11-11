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
Studio, or right-click the desktop) is the same thing with buttons. A change
applies at once in every open window; nothing needs reloading.

You can only bind keys to actions an app already declares; a new action is an
app change, outside this skill. Imbue Studio's own shortcuts (New window, Close
window, Quit, zoom) cannot be changed from here; for those, send the user to
Imbue Studio's Workspace settings > Keyboard shortcuts.

**Every change goes through this script. Never call the shell's keybinding
routes yourself**, with `curl` or otherwise, and never edit a file to change a
key: those save a key another action already has, and a key that can never
work, without a word. The script works out the refusals, the clashes and the
warnings from the same lists the Keyboard shortcuts screen uses, so do not judge
any of them from memory.

```bash
KEYS="python3 .agents/skills/edit-keyboard-shortcuts/scripts/edit_keyboard_shortcuts.py"
```

## 1. Find the action

```bash
$KEYS list
```

Each entry starts with the two words every other command takes, `APP ACTION`
(e.g. `system_interface open-launcher`), then the action's name as the user
knows it and the keys it has now. Match the user's words against the names;
never guess an id. If two actions could be meant, ask.

## 2. Spell the keys

Lowercase tokens joined with `+`: modifiers `ctrl`, `alt`, `shift`, `meta` in
any order, then one key. The key is a single character (`k`, `/`, `.`, `]`) or
one of `enter escape tab space backspace delete insert home end pageup pagedown
arrowup arrowdown arrowleft arrowright plus f1`...`f12`.

`meta` is the primary modifier: Command on a Mac, Ctrl on Windows and Linux. So
"Cmd+Shift+J" is `shift+meta+j`, and "Option-Command-O" is `alt+meta+o`. `ctrl`
is the Mac's Control key only. When the user says "Ctrl" and you do not know
they are on a Mac, they almost certainly mean `meta`; if you cannot tell, ask.

**Put every key in single quotes** on the command line (`'meta+;'`,
`'shift+meta+]'`): many keys are characters the shell would otherwise act on.

The script may rewrite a spelling to the one the keyboard sends: Shift with a
digit or a punctuation key is spelled with the character it types, so
`'shift+meta+]'` becomes `shift+meta+}`. The `KEYS:` line of the output shows
what was really checked and saved.

## 3. Save

```bash
$KEYS save APP ACTION 'CHORD'...          # these keys replace the ones it has
$KEYS save APP ACTION 'CHORD'... --add    # keep the ones it has and add these
$KEYS save APP ACTION --none              # the action has no shortcut at all
```

Add `--everywhere` only when the user asks for "everywhere" or "all my
workspaces": it saves to their account, which may show them a request to
approve in Imbue Studio the first time. Without it the save is for this
workspace only. With `--everywhere`, a key another action has is refused
outright (exit 2): taking a key works for this workspace only.

To see what a save would do without saving, run the same command with `check`
in place of `save`; it takes the same flags.

## 4. Act on what the script prints, by its exit code

The output is the whole verdict. Read the `RESULT:` line and every section
under it, and do what its exit code says:

| Exit | What happened | What you do |
| --- | --- | --- |
| 0 | Saved (or, for `check`, it can be saved). | Tell the user it is done, **and every line under `WARNINGS`**. |
| 2 | Refused. Nothing changed. | Tell the user each reason under `REFUSED`, as written, and offer another key. Do not try another route. |
| 3 | Another action has the key. Nothing changed. | Tell the user each line under `CLASHES` and ask whether to take the key. Only on a yes, run `save` (or `reset`) again with `--take`. |
| 1 | It could not run. Nothing was checked and nothing changed. | If the command was mistyped, fix it and run it again. Otherwise tell the user it could not be done and why. Do not save any other way. |
| 4 | Something changed, but the step did not finish. | Tell the user every line under `ALREADY CHANGED` and what went wrong, then run `list` and say what those actions have now. |

What is refused: a spelling that is no key; Ctrl+Alt+Del; for an action that
runs anywhere in the workspace, a key that typing would trigger; and **any key
an operating system keeps for itself**, such as Command-Space (Spotlight on a
Mac) or Alt+Tab (the window switcher on Windows and Linux). Such a key is
refused whichever platform the user is on, because a shortcut is saved for all
of them. The refusal names the operating system and what it uses the key for.
If the user still wants that key, they can set it themselves on the Keyboard
shortcuts screen; you cannot, and must not save it any other way.

Rules that hold every time:

- **Never pass `--take` before the user has said yes** to taking that key from
  that action, in this conversation. A take removes only the keys listed under
  `CLASHES`; a `TOOK ... FROM` line names each action that lost one, so tell
  the user.
- **Never drop, soften or summarise a `WARNINGS` line.** Each one names where
  the key will not work or will do something else: a browser that keeps it, a
  platform whose keyboard lacks it, or one of Imbue Studio's own shortcuts.
  You do not know which platform the user is on, so they need all of them. Say
  them when you report the save, not only if asked.
- A warning does not stop a save, and is not a reason to refuse the user. They
  may still want the key.
- After `--everywhere`, read the `ACCOUNT DELIVERY` line. Only `DELIVERED`
  means it reached the account; anything else means it is saved in this
  workspace only so far, and the line says what the user has to do.

## 5. Reset

```bash
$KEYS reset APP ACTION
```

This drops this workspace's own choice for the action. What answers then is
the user's account choice if they have one, otherwise the app's default; the
`RESULT:` line says which, so do not promise "the default" before you read it.
A reset can bring back a key another action has taken since: that is a clash
(exit 3), handled like any other, with `reset APP ACTION --take` only after the
user agrees. A reset prints `WARNINGS` for the keys it brings back, the same as
a save; tell the user every one.

## 6. Tell the user

Say it in their terms: the action's name, the keys as the script wrote them
(it gives the Mac spelling and the Windows and Linux one), any action that lost
a key, and every warning. Do not mention the script, routes, ids, or JSON.
