A failure that owes the person help can offer "Report a bug", which opens Imbue Studio's bug report with the failure attached.

- "Report a bug" is offered only where the embedder said `opensHelp` in its capabilities, so it stays hidden in a shared visit, the hosted web client and an older desktop app. The report carries the failure it is about: the sentence on screen and what its "See details" holds, attached as the bug report's details (`minds:open-help`'s `errorDetails`, cut to the embed contract's 16384 characters).

- App pages learn it from the shell: the handshake now says `canReportBug`, and the connection gains `canReportBug()` and `reportBug({message, details})` (which sends the new `shell:report-bug` with that failure).

- New pieces for the screens that adopt them: the `ReportBugButton` component, which renders nothing where it could not work and takes the failure it reports, and `report-bug.ts`, which the shell opens the bug report with.

- A lasting toast whose failure owes help says so (`isHelpOwed`), and the toast stack draws "Report a bug" on it whenever the page passes `onReportBug`, decided as it draws, so a toast that went up before the page knew it could open a bug report gains the button once it does. "Report a bug" leaves its toast up once pressed, so the toast's other buttons still apply.
