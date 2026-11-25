A failure toast on the workspace desktop that owes the person help (anything but a request nothing answered, a refusal with only a reason, or one the app called routine) offers "Report a bug" where the desktop app opens its bug report.

- "Report a bug" is decided as the toast draws, so a toast shown before the desktop app said it opens one (a failed startup read) gains the button once it does, and the report it opens carries the toast's sentence and details. Pressing it leaves the toast up.

- App pages can offer "Report a bug" too: the shell tells each page in its handshake whether the desktop app opens its bug report, tells pages already open again when the desktop app says so after they loaded, and opens the bug report when a page asks with `shell:report-bug`, attaching the failure the page sent (or nothing, for a payload that holds none).

- Only built-in apps (`system = true` in their `app.toml`) offer it: a user's own app's faults are not Imbue's bugs, so its pages are told they cannot report one, a `shell:report-bug` they send is ignored, and a toast about a launch the app refused or could not answer offers none (it still stays up).
