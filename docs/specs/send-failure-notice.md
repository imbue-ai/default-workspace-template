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

---

# Recovering from a failed send, not just being told about it

**Status: implemented.** What follows is the design as built.

The notice above reports. This section gives it actions, so the user can resolve the failure from the chat instead of being handed a sentence and a dead end.

## Generalizing

The three actions are not dialog-specific and must not be. Any send can fail -- a dialog holding the input, a readiness timeout, an unconfirmed submission, a transport error -- and in every one of those cases the same three things are worth offering. What differs is only the explanation, which the backend already supplies in the harness's own words.

So the notice keeps one shape for every failure:

- **Title** -- "Couldn't send your message".
- **Body** -- the failure text as received, unchanged. This is the part that varies, and it is the reason the actions can be uniform: the user is told what went wrong, then offered the same three ways out.
- **A line inviting them to fix it themselves**, naming the agent's terminal. Many failures (an unsubmitted shell command, a dialog needing a real answer) are resolved in seconds by someone looking at the pane, and the current notice never says so.
- **Actions** -- Cancel, Retry, Force.

**What decides whether actions appear is the operation, not the failure.** A failed *send* is repeatable, so it gets all three. A failed *interrupt* -- which shares this notice today -- is not a send: retrying it means retrying the interrupt, and forcing it is meaningless. So the notice takes an optional recovery descriptor; an operation that supplies none renders exactly today's single OK button.

## The actions

### Cancel

Closes the notice and returns the user to the composer with their message text restored, **prepended** to whatever is already there, and their attachments restored.

Today's catch block already restores text, but only when the composer is empty -- it deliberately refuses to clobber a newer draft typed during the in-flight send. Prepending is what makes that guard unnecessary: the failed message goes in front, the newer draft follows, nothing is lost either way. Separate the two with a newline so they do not run together into one line.

### Retry

Runs the same send again, with the same text and attachments, from the same code path. No special casing: it is the ordinary send, so it re-runs preflight and can fail again, re-opening this notice with whatever the new failure says.

Retrying the exact thing that just failed will usually fail again -- that is expected and worth keeping, because the common case it does fix is a failure the user resolved by hand in the terminal before clicking it. The button should read as "I've dealt with it, try again", so keep it as the second action, after Cancel.

While the retry is in flight, disable all three buttons rather than closing the notice, so a double-click cannot start two sends.

### Force

Restarts the agent, then sends the message.

- Tooltip: **"Restarts the agent and sends the message"**.
- The restart already exists: `POST /api/agents/<id>/interrupt` runs `mngr start <agent> --restart --no-resume`, which stops the agent, ending any in-progress turn, and starts it fresh without a resume message. That is precisely "stop the agent, start the agent".
- On success, send the message exactly as Retry does.
- If the restart itself fails, the notice stays open and shows the restart's error instead. Do not attempt the send.

**Force is destructive and must read that way.** It ends whatever turn the agent was in the middle of, and that work is not recoverable. Give it the visual weight of a destructive action -- last position, distinct styling -- rather than making it the easiest button to reach. It exists because the failures worth forcing (a wedged pane, a surface mngr cannot name) are exactly the ones where nothing gentler works.

The `is_primary=true` refusal on that endpoint applies unchanged: the services agent must never be restarted this way, and the endpoint already refuses. Surface that refusal as the notice's new body if it happens.

## Implementation

### Frontend -- `MessageInput.ts`

- Replace `actionFailureDetail: string | null` with a small record: the detail, and an optional recovery describing how to repeat the operation (the text and attachments to resend, and the agent id). Component-closure state, as now.
- The send catch block populates the recovery; the interrupt catch block does not, so it keeps a single OK button.
- **Restore the composer in the catch block, immediately.** The recovery record is closure state, so holding the message only there loses it to a reload or a closed tab -- the box is where it waits. Retry and Force remove that copy once the send has actually landed, taking care that Force has by then prepended the drained queue block above it.
- Render two or four buttons off the same markup already used, with the focused default on Cancel, since it is the only non-acting choice. Escape and backdrop-click do what Cancel does, including restoring the text -- dismissing must never lose the message.
- Disable the buttons while a retry or force is in flight; show which one is running.

### Backend

No new endpoint. Force composes the existing interrupt and message endpoints, in that order, from the frontend -- keeping the "stop, start, send" sequence visible where the user triggered it rather than hidden behind a new fused route that would need its own partial-failure semantics.

## Testing

- A failed send opens the notice with three actions; a failed interrupt opens it with one.
- Cancel restores the failed text prepended to an existing draft, with attachments, and closes.
- Escape and backdrop-click behave as Cancel, including the restore.
- Retry re-invokes the send with the original text; a second failure re-opens the notice with the new text.
- Force calls interrupt then send, in that order; a failed interrupt shows the restart error and does not send.
- Buttons are disabled while an action is in flight.

## Open question

Whether Retry should be offered at all for a failure that cannot plausibly resolve itself (a pending shell command needs a human either way). Offering it uniformly is simpler and never wrong -- the user is the one who decides whether they have fixed it -- so start uniform and only special-case if the button proves misleading in practice.
