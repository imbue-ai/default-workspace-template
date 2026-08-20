# Surfacing a failed send in the workspace, not in a system alert

## The problem

When a send fails, `MessageInput.ts` ends its catch block with:

```ts
alert(`Failed to send message: ${detail}`);
```

That is the browser's native alert, which in the desktop app is an OS-chrome modal with no title, no styling, and no relation to the workspace it interrupts.
It is also the only feedback: the composer text is restored and the failure is logged to the console, but the alert is what the user sees.

The text inside it is whatever `describeRequestError` extracted from the response -- and that was never the reason the send failed. `send_to_agent` returned a bool, discarding `MessageResult.failed_agents`, so the only bodies the UI could receive were `...is not ready to receive messages yet` (503) and `Failed to send message to agent 'Chat 2' (0 successful agents)` (500). A send refused because a dialog held the agent's input said nothing about the dialog.

The workspace already has a better surface for exactly this.
The declined-command notice (`MessageInput.ts`, the `custom-url-dialog` markup) is an in-app modal with a title, a body, and a focused **OK** button that Enter, Space, and Escape all dismiss.
A failed send should use that, not the OS.

## Scope

Every failed send, whatever the cause -- a dialog holding the agent's input, a readiness timeout, an unconfirmed submission, a transport error.
The notice is not dialog-specific and must not be: the point is that the user is told what happened in the app, in the same shape every time.

Out of scope: `QueuedMessageView.ts`'s failed-resend alert and the `DockviewWorkspace.ts` sites (rename, pin, open terminal, create chat). Same problem, each its own flow; doing them together would make this hard to review.

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

**Both halves were needed.** A styled notice reading "(0 successful agents)" is no more useful than the alert it replaces, so the reason is now carried end to end:

- `MngrMessenger.send_to_agent` returns `str | None` -- None when delivered, otherwise the harness's own words, taken from the first entry in `failed_agents`.
- `AgentManager.send_message_to_agent` passes that through. Its other callers (model switch, queue flush, tap recovery, welcome resend) check `is None` instead of a bool; the welcome resend now logs *why* it failed.
- The chat path alone turns a reason into `SendFailedError`, because `SessionDeps.send_to_harness` is typed as returning a bool and is shared with paths that have nowhere to show a reason. The session's send already treats an exception as an expected exit -- it resolves its in-flight record in a `finally` and lets the request fail with the draft kept -- so nothing else changed.
- The send endpoint catches it and answers 500 with that detail.

A richer `{title, detail}` shape is still possible later; it is not needed for the reason to arrive.

## Implementation

### `system/apps/system_interface/frontend/src/views/MessageInput.ts`

- Add **component-closure** state (`let actionFailureDetail: string | null`), not module state: every open chat panel mounts its own `MessageInput`, so a module-level value would raise the notice in all of them at once.
- Replace the `alert(...)` at the end of the send catch block with a call that sets that state and redraws.
- Render it with the same `custom-url-dialog` markup the declined-command notice uses: `h3.custom-url-dialog-title`, `p.logout-notice-body`, and a focused `button.custom-url-dialog-cancel` labelled OK.
- Give the overlay **its own** Escape handler. The existing one is registered by the declined overlay's `oncreate` and clears only that state; sharing one function reference would be de-duplicated by `addEventListener` and then torn down by whichever overlay closed first.
- Guard the post-send `requestAnimationFrame(focusMessageTextarea)`. It lands after the notice has mounted and focused its OK button, so refocusing the composer would steal it -- leaving a modal the keyboard cannot dismiss and an Enter that re-sends the restored text. Refocus from the dismiss handler instead.
- Set the notice only when `currentAgentId === agentId`: the catch runs after an await, so the user may already have switched, and the switch-clear has gone by.
- Take `MessageInput.ts`'s other `alert` (a failed interrupt) too -- it is in the same closure, and leaving one system alert beside a styled notice is worse than either.
- Give `.logout-notice-body` `overflow-wrap: anywhere`, `max-height: 40vh`, `overflow-y: auto`. A proxy or an unhandled route answers with a whole HTML page, which mithril hands over verbatim; without this the dialog widens past the viewport or pushes its OK button off-screen. `alert()` handled that natively.
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
