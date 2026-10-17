Failure toasts on the workspace desktop can now be read and acted on.

- Each failure says whether its toast stays: a failure whose retry is the control just pressed (closing a window, moving or removing a shortcut, a link) leaves a plain note that goes after five seconds; a launch, a failed startup read, a quit and a page's own move leave a toast that stays until dismissed, with a close control, a collapsed "See details" holding the status and the server's own text, and "Report a bug" where the desktop app opens its bug report. A failure whose sentence asks for a bug report always stays, wherever it happened. The same toast twice replaces the first, and at most three lasting toasts stay on screen.

- A failed launch names the app by its display name: an app's refusal reads "Docs couldn't open that.", and an app that could not answer reads "Docs couldn't open that. Try again. If this persists, report a bug.", with the app's reason behind "See details"; an app's own sentence for people is shown as written, however long. The shell now passes the app's reason on for any status and writes no sentence of its own. A window that could not open names its app by its display name too.

- Failures no longer read "Could not close the window: <the shell's reason>": the reason goes behind "See details" on a toast that stays, and to the browser console under a plain note, and a request that got no answer says so on its own.

- App pages can offer "Report a bug" too: the shell tells each page in its handshake whether the desktop app opens its bug report, tells pages already open again when the desktop app says so after they loaded, and opens the bug report when a page asks with `shell:report-bug`. A page that connects to the shell after its window loaded (one that loads the app contract with a dynamic `import()`) is now greeted when it says its capabilities, so it learns it has a shell and can show these buttons; before, it never heard a handshake.

- A preview shell's refusal ("This is a preview of a proposed change; it cannot change the live workspace.") is sent as a sentence for people, so a screen that words failures shows it as written.
