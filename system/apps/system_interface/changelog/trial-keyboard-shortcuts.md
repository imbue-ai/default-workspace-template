The keyboard shortcuts screen is redesigned. Each app's actions sit in a collapsible section with a count. Every shortcut is one keycap written the way a menu writes it ("⌘⇧N" on an Apple platform, "Ctrl+Shift+N" elsewhere, named in words on hover), with "or" between an action's alternatives. An action saved or reset is marked "Saved" until the screen is opened again.

Editing one action opens its editor below the row:

- Each of its shortcuts can be removed on its own, and "Add shortcut" records another. The recorder takes every key, Escape included, and has a Cancel of its own. Removing them all turns the action off, which the list shows as "Off".

- No step leaves one shortcut on two actions of an app without asking. Recording a shortcut another action uses, "Restore default", a row's "Reset", and "Discard" on a change still waiting to reach the account each offer "Replace" or "Cancel" when what they would bring in is in use. Replace takes it off every action that has it, at once: that row shows its remaining shortcuts marked "Changes when you save", the editor says what will be removed, and the two are saved together. A shortcut that still runs two actions (from the account's settings, say) is flagged on both rows. A row offers "Reset" only when resetting would change what the action runs.

- A shortcut Imbue Studio or the browser keeps for itself (Command-W, Command-T, and the like) is saved with a warning that it may not reach the workspace.

- The "Only in this workspace" and "Everywhere" saves (a visitor's single "Save") are unchanged.

Saving keeps every shortcut the action has, where before a save replaced them all with the one just recorded. The catalog (`GET /api/keybindings/catalog`) answers each action's `reset_chords`, what a reset of this workspace's change leaves, and each unsent save's `restored_chords`, what discarding it leaves. A save that fails partway reloads the screen, so it shows what was saved. While a recorder listens, the shell ignores the close-window chord Imbue Studio relays, so pressing it then closes no window. Changes waiting to reach the account are listed by app and action name rather than by id.

The shell also serves the screen as a page of its own at `/keyboard-shortcuts`, which Imbue Studio's Workspace settings frames. The page paints no background and does not clip, and it reports its height and whether its recorder is listening with the new `minds:keyboard-shortcuts-page` message. The desktop's "Keyboard shortcuts..." menu item opens that Workspace settings group when Imbue Studio says it can (`opensKeyboardShortcuts`), and otherwise, in a plain browser, the same screen in a dialog, now wider and scrolling.
