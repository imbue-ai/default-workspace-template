The keyboard shortcuts screen is redesigned. Each app's actions sit in a collapsible section with a count, every chord is drawn as keycaps (the Mac's modifier symbols on an Apple platform, spelled-out names elsewhere, with the chord in words on hover), and an action that differs from its default carries a "Changed" marker saying where the change lives.

Editing one action opens its editor below the row:

- Each of its chords can be removed on its own, and "Add shortcut" records another (Escape cancels). Removing them all turns the action off, which the list shows as "Off".

- A recorded chord another action of the same app already uses is offered "Replace", which moves it to this action and saves both, or "Cancel".

- A chord Imbue Studio or the browser keeps for itself (Command-W, Command-T, and the like) is saved with a warning that it may not reach the workspace.

- "Restore default" puts back the app's chords, and the "Only in this workspace" and "Everywhere" saves (a visitor's single "Save") are unchanged.

Saving keeps every chord the action has, where before a save replaced them all with the one just recorded. Changes waiting to reach the account are listed by app and action name rather than by id.

The shell also serves the screen as a page of its own at `/keyboard-shortcuts`, which Imbue Studio's Workspace settings frames. The page tells Imbue Studio it has loaded with the new `minds:keyboard-shortcuts-ready` message. The "Keyboard shortcuts..." entry on the desktop's context menu keeps opening the same screen in a dialog, now wider and scrolling.
