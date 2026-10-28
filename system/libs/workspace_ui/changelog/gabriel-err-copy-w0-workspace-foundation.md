A failed request inside a workspace is now told the same way everywhere: a sentence of ours on screen, and the status and the server's own text behind a collapsed "See details".

- One reader for every failed request, REST helper or chat request alike: a route's `user_facing_message` (a sentence written for people, shown as written), else its `detail` (a reason, kept behind "See details"), else no answer (nothing answered, or a gateway status with no reason of the server's), else nothing at all.

- One function words it, from what the screen says failed: a request nothing answered says "The workspace didn't answer. Try again in a moment." for a read and "The workspace didn't answer, so Imbue Studio can't tell whether this went through." for a change, never prefixed with what failed; a refusal with a reason says what failed ("This window couldn't be closed."); anything else says what failed, then "Try again. If this persists, report a bug.". These replace "HTTP 500", "request failed (HTTP 503)", "could not reach the workspace", "unknown error" and the gateway sentence that claimed "nothing was changed", which the workspace cannot know: Imbue Studio's forward proxy can send a gateway status after the request arrived.

- A server's own sentence asks for a bug report only when its answer says `reportable: true`.

- A screen not yet worded this way keeps showing the server's own reason: a failed request's error message, and `describeRequestError`, are still the route's `detail`, with our sentence only where the route gave none.

- "Report a bug" (which opens Imbue Studio's bug report) is offered only where the embedder said `opensHelp` in its capabilities, so it stays hidden in a shared visit, the hosted web client and an older desktop app; the sentence still says "report a bug". App pages learn it from the shell: the handshake now says `canReportBug`, and the connection gains `canReportBug()` and `reportBug()` (which sends the new `shell:report-bug`).

- "Ask an agent to fix this": `askAgentToFix(connection, {whatFailed, details})` in the app contract starts a chat whose first message says what failed, quotes the details as they are, and asks the agent to fix it and say what it changed, through the shell's `shell:start-with-text`. Plain-HTML pages use it from `/_static/app_contract.js`.

- New pieces for the screens that adopt them: `SeeDetails` (also drawn by `NoticeDialog` from its new `details`), the `AskAgentToFixButton` and `ReportBugButton` components (each renders nothing where it could not work), and `report-bug.ts`, which the shell's own toasts open the bug report with.

- The toast stack (`components/toasts.ts`) draws lasting toasts: a toast that stays until dismissed, with a close control, a collapsed "See details", and its buttons ("Try again", "Report a bug"), which the queue (`models/toasts.ts`) holds at most three of. A plain toast takes no press unless the page sets `closesPlainToasts`, which gives it a close button.

- `requestJson`, `getJson` and `postJson` throw the same `HttpError` for a refusal and for a request nothing answered, a GET read as a read and any other method as a change.
