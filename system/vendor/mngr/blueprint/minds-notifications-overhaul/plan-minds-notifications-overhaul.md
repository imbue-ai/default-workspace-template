# Plan: minds notifications overhaul

Builds on `blueprint/minds-notification-system/` (the v1 feed, requests only) and supersedes its "other producers do not join the feed" and "browser-mode delivery" decisions. Closes GitHub issue #740. Paired with a default-workspace-template change (the notify skill and the focus-chat embed message).

## Overview

- The feed becomes the single notification pipeline. Every notification is a feed entry of one kind: permission request, agent message, or system event. The bell, dock badge, toasts, and OS banners all read from it. The three direct-to-OS producers (agent API, backup setup failure, backup cleanup result) stop bypassing it.
- The OS-banner focus gate is fixed at its signal. The renderer only re-sent its focus state on the top-level window's own focus and blur events, which Chromium does not fire when keyboard focus sits inside the workspace iframe (proven with an Electron 40 probe). Electron main, whose window focus events fire reliably, now relays focus to the renderer. The gate rule itself stays: no OS banner when a focused window shows the asking workspace.
- Agents in chats get a sanctioned way to tell the user a turn is finished: a template skill wrapping the existing minds API route, plus a Stop hook that asks for a notification at the end of every turn and lets the agent decline by saying nothing. The agent writes the one-sentence summary. Clicking the notification lands in that chat.
- Delivery is desktop-only. Browser mode is a dev path, not a product, so its Web Notification path, permission hint, and the osascript and tkinter fallbacks go. In-app toast cards flash in every open window (issue #740), with no focus gate and no catch-up queue.
- "Couldn't open link" stops being a system notification and becomes an in-app toast.

## Expected behavior

Feed and kinds:

- Every entry has a kind, a workspace (empty only for the account-level backup cleanup result), a headline, a body, and a timestamp. Unresolved entries sort first, newest first; resolved requests remain as approved/denied/closed receipts below them.
- A permission request behaves as today: enters the feed when filed, resolves on approve/deny, closes when its request vanishes, reopens if it reappears. Clearing it hides it from the feed and the badge; the request stays pending in the inbox and in-chat card.
- An agent message enters the feed when the app receives the API call. It resolves when clicked, when cleared, when the user navigates to its workspace, or as closed when its workspace leaves the list. Reading or clearing removes it from the feed; it never becomes a receipt.
- A system event (backup setup failed for a workspace; backup cleanup finished or failed for the account) enters the feed when the app records it. Clicking a workspace-scoped one opens that workspace's backups page; clicking the account-level one opens the backup quota surface the cleanup was started from. Either click resolves and removes it. Clearing removes it.
- Every entry has a clear button. The feed has clear-all, which removes everything, receipts included.
- The feed stays in-memory. Agent messages and system events are lost on restart; requests are re-delivered by the gateway as before.
- The Notifications feed gets filter tabs: all, requests, messages, system.

Counts and dots:

- The bell badge and the dock badge count every unresolved entry of any kind.
- The per-workspace red dot on the key tab stays requests-only.

In-app toasts (style `cards` or `both`):

- A fresh unresolved entry of any kind flashes a toast card in every open window immediately, including for the workspace on screen and including unfocused windows. No flash while the feed overlay is open in that window. A toast in a background window may expire on its own 15-second timer before the reader returns; the bell and the OS banner cover that case.
- Clicking a toast does what clicking the feed row does. The toast's X retires the flash only.

OS banners (style `os` or `both`, master toggle on):

- Layout follows Slack's: title is the workspace name, subtitle is the headline (chat name, request title, or event name), body is the detail. The account-level cleanup result titles itself with the account.
- No per-workspace grouping in Notification Center (Electron's `Notification` exposes no group identifier), no action buttons and no persistent alert style.
- A banner fires for a new entry unless a focused window is currently showing that entry's workspace. Being shown in an unfocused window does not suppress it. Account-level entries always fire. Startup backfill stays silent.
- The suppression decision uses a focus signal relayed from Electron main's window focus and blur events, so it is correct while keyboard focus is inside the workspace iframe and after switching to another app.
- Clicking a banner focuses the window showing that workspace (or navigates the most recent window) and opens the review popup for a request, the chat for an agent message, or the backups surface for a system event.
- Settings keeps the master toggle, the style choice, an "Open system notification settings" link, and gains a "Send test notification" button that pushes one banner through the real path.

Agent messages from chats:

- The template ships a skill for chat agents. Its description says it is for chats and not for workers. Its guidance: notify at the end of every turn in which the agent actually did work, with a one-sentence summary of what was done. Not only after long tasks -- the user cannot tell from outside how long a turn took, and nothing else tells them it finished. Chitchat, a single-line acknowledgement, a trivial answer, a question put to the user and a one-read reply are exempt.
- A Stop hook asks for one at the end of **every** turn, and the agent decides. Which turns are worth a notification is not decidable from outside -- a `tk` step record is a decent proxy and still wrong on the turns that matter -- so the hook does not try: the message itself carries the way out, "output nothing at all and stop", and the agent must not narrate that decision to a user who never saw the question. The cost is one short continuation on a turn that wants no notification.
- The hook gates on the agent's role, so workers, automations and subagents are never asked. It reaches the model by refusing the stop, which means the harness must also let the hook recognise the continuation it caused; claude's `stop_hook_active` does that and is the whole of the hook's state. codex acts on the refusal but is not known to report the continuation, so it stays unwired until that is measured; pi and agy have no stop channel that reaches the model at all. On every harness the rule is also in `AGENTS.md`.
- The skill's utility posts to the existing per-agent notifications route through the latchkey gateway's minds API proxy. If the app cannot be reached, the utility reports the failure so the agent can say in its reply that the notification did not go out.
- The route keeps its path. The body is free text; an optional title becomes a "Title: " prefix on the body; urgency is dropped. The app derives the workspace and chat names itself. No server-side throttle.
- Clicking an agent message navigates to its workspace and asks the workspace, over a new embedder-to-workspace embed-contract message, to focus that chat. Where the chat lands is the workspace's own choice: it raises a window already showing the chat, wherever it is; otherwise it points the viewer's pinned chat window at the chat; otherwise it opens the chat in a window of its own. The workspace announces, over a second new message, that its endpoint is listening, and the chrome holds the ask until it does -- a send into a document whose listener is not up yet is lost. The ask is fire-and-forget from there: a workspace on an older template announces nothing, never receives it, and just lands on the workspace.

"Couldn't open link":

- When Electron finds no handler for a mailto or tel link, the address is still copied to the clipboard and the window where the click happened shows an in-app toast saying so. No system notification, no feed entry.

Removed:

- Browser-mode OS delivery (Web Notifications from the renderer), the "Enable system notifications?" hint, its dismissed-flag preference, and the osascript and tkinter dispatch channels. A bare `minds run` outside Electron delivers nothing to the OS.
- The in-app card focus gate and the catch-up flash queue (issue #740).

Acceptance checks:

- With keyboard focus inside a chat, switch to another app, then have the agent file a request: an OS banner appears. Switch back with focus still in the chat and file another: no banner, a toast, and a bell entry. Both outcomes are visible in the backend's per-request gate log lines.
- An agent message from a chat produces a bell entry and a badge count; clicking it lands in that chat's tab, in the right project. Navigating to the workspace by other means clears its agent messages.
- With style `cards`, a backup setup failure shows a toast and a bell entry and no OS banner. With style `os`, it shows a banner and a bell entry and no toast.
- Two open windows both flash a card for one new request; neither queues anything for later.
- A mailto link with no handler shows an in-app toast and nothing in Notification Center.
- The "Send test notification" button produces a banner; its show or failure is logged.

## Changes

Desktop client backend (`apps/minds/imbue/minds/desktop_client`):

- Generalize the feed from a request reconciliation into a store of kinded entries. Requests keep their reconcile-from-pending derivation; agent messages and system events are appended directly by their producers and resolved by the rules above. Add clear, clear-all, and navigate-to-workspace resolution paths for the renderer to call.
- Route the three direct producers into the feed: the agent notifications route appends an agent message; backup setup failure and backup cleanup result append system events. Drop urgency from the route's body model and API schema; add the optional title prefix.
- Reduce the dispatcher to the Electron channel: remove the osascript and tkinter paths and the browser-mode double-delivery guard. Carry the Slack-style title, subtitle, and body in the stdout event.
- Apply the OS-banner gate uniformly per kind: workspace-scoped entries consult the focused-window reader; account-level entries skip it. Keep the startup-backfill silence for requests.
- Remove the OS-hint-dismissed preference from the config and the settings read and write models. Add a test-notification endpoint that dispatches one banner.
- Update the bundled bug-report and diagnostics expectations wherever they list notification preference fields.

Electron (`apps/minds/electron`):

- Relay each window's focus and blur to its renderer over the preload bridge. Render the new title, subtitle, and body in the native notification. Keep the click routing, and extend it so agent-message and system-event deep links land on the right surface.
- Replace the "Couldn't open link" native notification with a message to the originating window's renderer that shows a toast; keep the clipboard copy.
- Keep the existing open-notification-settings IPC. The "Send test notification" button needs no IPC of its own: it posts to the backend's test-notification endpoint, which dispatches through the same stdout path as a real banner.

Frontend (`apps/minds/frontend`):

- Re-send client state on the relayed main-process focus signal instead of only the window's own focus and blur events; keep refreshing prefs on focus gain.
- Delete the card focus gate and the catch-up queue (issue #740). Delete the renderer Web Notification path, the OS permission request on style change, and the hint under the bell.
- Render kinds: per-row kind marks, filter tabs on the Notifications page, clear buttons per row, clear-all, and click handling per kind (review popup, chat deep link with the focus-chat message, backups surfaces). Add the transient "Couldn't open link" toast as a non-feed toast in the toast layer. Add the test-notification button in Settings.
- Resolve a workspace's agent messages when the route lands on that workspace.

Embed contract (`apps/minds/docs/embed-contract.md` and the template's `workspace_ui` contract):

- Add an embedder-to-workspace focus-chat message carrying the chat's agent id, with the view and project selection rules above. Bump the contract version.

Default workspace template (paired PR):

- Add the notify skill for chat agents with the guidance above and a utility that posts through the minds API proxy and reports unreachability.
- Handle the focus-chat message in the system interface: open the tab if needed, choose the view per the rules above.

Docs and changelogs:

- Update `apps/minds/docs/desktop-app.md` and `docs/latchkey-permissions.md` where they describe notification routing and browser mode. Update the previous blueprint with a pointer to this one.
- Changelog entries in `apps/minds/changelog/` and `dev/changelog/` for this branch, and in the template's `system/changelog/`.

Tests:

- Feed: kinded entries, per-kind resolution rules, clear and clear-all, navigate-to-workspace resolution, cap eviction across kinds.
- OS gate: per-kind suppression, account-level always fires, the fixed focus signal (a client state that reports unfocused after the relay, with the iframe-focus scenario modeled).
- Producers: the API route and both backup producers land in the feed with the right kind and fields; the `cards` style yields no dispatch.
- Renderer: cards flash in every window regardless of focus (replacing the catch-up suite), kind filters, clear actions, deep-link routing per kind.
- Electron: notification payload rendering and click routing per kind, the link-fallback toast message, focus relay wiring.
- Template: focus-chat message handling and the notify utility's failure reporting.
