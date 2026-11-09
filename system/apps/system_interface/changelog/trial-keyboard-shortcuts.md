The keyboard shortcuts screen is redesigned. Each app's actions sit in a collapsible section with a count. Every shortcut is one keycap written the way a menu writes it ("⇧⌘N" on an Apple platform, "Ctrl+Shift+N" elsewhere, named in words on hover), with "or" between an action's alternatives. An action that differs from its default carries a "Changed" marker saying where the change lives.

Editing one action opens its editor below the row:

- Each of its shortcuts can be removed on its own, and "Add shortcut" records another. The recorder takes every key, Escape included, and has a Cancel of its own. Removing them all turns the action off, which the list shows as "Off".

- A recorded shortcut another action of the same app already uses is offered "Replace" or "Cancel". Replace takes it off the other action at once: that row shows its remaining shortcuts marked "Changes when you save", the editor says what will be removed, and saving saves both actions.

- A shortcut Imbue Studio or the browser keeps for itself (Command-W, Command-T, and the like) is saved with a warning that it may not reach the workspace.

- "Restore default" puts back the app's shortcuts, and the "Only in this workspace" and "Everywhere" saves (a visitor's single "Save") are unchanged.

Saving keeps every shortcut the action has, where before a save replaced them all with the one just recorded. While a recorder listens, the shell ignores the close-window chord Imbue Studio relays, so recording it closes nothing. Changes waiting to reach the account are listed by app and action name rather than by id.

The shell also serves the screen as a page of its own at `/keyboard-shortcuts`, which Imbue Studio's Workspace settings frames. The page paints no background and does not clip, and it reports its height and whether its recorder is listening with the new `minds:keyboard-shortcuts-page` message. The "Keyboard shortcuts..." entry on the desktop's context menu keeps opening the same screen in a dialog, now wider and scrolling.
