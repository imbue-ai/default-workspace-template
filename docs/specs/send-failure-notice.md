# Surfacing a failed send in the workspace, not in a system alert

## The problem

When a send fails, `MessageInput.ts` ends its catch block with:

```ts
alert(`Failed to send message: ${detail}`);
```

That is the browser's native alert, which in the desktop app is an OS-chrome modal with no title, no styling, and no relation to the workspace it interrupts.
It is also the only feedback: the composer text is restored and the failure is logged to the console, but the alert is what the user sees.

The text inside it is whatever `describeRequestError` extracted from the response, so a send blocked by a Claude Code dialog arrives as one long sentence in a dialog that looks like a crash report.

The workspace already has a better surface for exactly this.
The declined-command notice (`MessageInput.ts`, the `custom-url-dialog` markup) is an in-app modal with a title, a body, and a focused **OK** button that Enter, Space, and Escape all dismiss.
A failed send should use that, not the OS.

## Scope

Every failed send, whatever the cause -- a dialog holding the agent's input, a readiness timeout, an unconfirmed submission, a transport error.
The notice is not dialog-specific and must not be: the point is that the user is told what happened in the app, in the same shape every time.

Out of scope: the other `alert()` call sites in `DockviewWorkspace.ts` (rename, pin, open terminal, create chat). They have the same problem and should follow, but each is its own flow; doing them together would make this one change hard to review.

## Behavior

- A failed send opens an in-app notice with a title, the failure text as the body, and an OK button.
- OK, Enter, Space, and Escape all dismiss it. The button takes focus when the notice opens, so it is reachable without a mouse.
- Dismissing changes nothing else: the composer text and attachments are already restored by the existing catch block, and that behaviour is untouched.
- The notice names the agent when the failure is agent-specific, since a workspace can have several chats open and the failing one may not be the visible one.
- Nothing is optimistically committed: this path already runs only after the backend has refused, so the "Sending..." bubble is dropped exactly as it is now.

## Title and body

The body is the failure text as received. The title is the part worth designing, because the body is often a full sentence already.

- Default title: **"Couldn't send your message"**. Plain, true for every cause, and it does not claim to know why.
- When the failure carries a short name for what went wrong, use that instead -- "Pending shell command", "Permission dialog" -- with the sentence as the body. That reads as a labelled explanation rather than a wall of prose.

mngr already keeps those two apart. `DialogDetectedError` is constructed from a nickname and the dialog's own message, and holds the nickname on the exception as `dialog_description`.
The message it raises then flattens both into one string, and the API passes that string on, so the frontend has no way to recover the pieces.

**This is the one piece of plumbing the change needs.** Two options:

1. **Frontend-only (ship this first).** Always use the default title and put the whole string in the body. No API change, no mngr change, and it already replaces the OS alert with something styled and dismissible. The result is a correct, plain notice.
2. **Structured (follow-up).** The send endpoint returns the failure as `{title, detail}` rather than one string, sourcing `title` from the error's short name where it has one. `describeRequestError` learns to read that shape and fall back to today's flattened string when it is absent, so old and new backends both work.

Option 1 is the whole user-visible win. Option 2 only improves the wording, and it costs a response-shape change across mngr, the system interface, and the frontend -- worth doing, but not worth blocking on.

## Implementation

### `system/apps/system_interface/frontend/src/views/MessageInput.ts`

- Add module state for the notice, mirroring the declined-command notice: the text to show, the agent it belongs to, and a dismiss function.
- Replace the `alert(...)` at the end of the send catch block with a call that sets that state and redraws.
- Render it with the same `custom-url-dialog` markup the declined-command notice uses: `h3.custom-url-dialog-title`, `p.logout-notice-body`, and a focused `button.custom-url-dialog-cancel` labelled OK.
- Reuse the existing Escape handling rather than adding a second key listener.
- Clear the notice when the visible agent changes, exactly as the declined-command notices already do -- a failure that names another agent's send must not follow the user into a different chat.

### Tests -- `MessageInput.test.ts`

- A rejected `sendMessage` opens the notice and does not call `alert`.
- The notice shows the failure text from `describeRequestError`.
- OK dismisses it; the composer still holds the restored text afterwards.
- Switching agents clears it.
- A successful send opens no notice.

## Why not a harness-declared popup

`HarnessSpec.popups` exists and is the natural-looking home, but it is the wrong mechanism.
Its two triggers, `composer_command` and `turn_check`, both fire in the frontend from what the user typed or what the agent's state says, before anything is sent.
A send failure is the opposite: it is the backend reporting, after the fact, that something refused.
Declaring it per harness would also be wrong on its face -- every harness can fail a send, and the failures worth showing are not harness-specific.

If the structured follow-up lands, the harness still contributes nothing to this path: the title comes from the error, which the harness plugin already wrote.
