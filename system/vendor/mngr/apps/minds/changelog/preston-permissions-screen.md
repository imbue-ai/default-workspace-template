Added a per-machine Permissions screen to the workspace options panel, matching the minds-options design prototype: a new Permissions tab (key icon) joins Share machine and Machine settings in the titlebar icon-tabs and the docked options overlay.

The screen renders every permission as a toggle, grouped per connection: one left-nav entry per connected (service, account) pair with grouped per-permission switches (Full access / per-area groups / an unrestricted-access Extras group), an Add connection entry that runs the existing browser sign-in for services with no account yet, plus Local files (shared-path grants) and Other machines (cross-workspace management verbs) sections.

Flipping a toggle posts the single flip; the desktop client recomputes the affected rule's complete permission set server-side and writes the full set through the latchkey gateway's permissions extension (never a diff). Turning a connector rule's last permission off deletes the rule; latchkey-self rewrites preserve unrelated baseline permissions, and revoked path/verb grants whose schemas are still in the host file can be toggled back on. Each connection also gets a Revoke all action scoped to this machine.

The Permissions pane leads with a "Waiting on you" strip when this machine's agents have pending permission requests: plain rows (service mark, title, the agent's reason, oldest first, first three visible with a "+N more" fold) that open the shared review popup on that request.

Permission requests are now popup-only, matching the prototype: the inbox drawer and the titlebar Requests button (with its count badge) are gone, along with the auto-open setting and its routes. A new pending request always auto-opens a centered popup headed "Permission request for [dot] <workspace>"; resolving advances to the next pending request and the popup dismisses itself when none remain. The chat card's "Review & respond" opens the same popup.

The grant dialog itself was restyled to the prototype's review design: service brand mark + title, an Account section (radio list), a "Reason" section, an "Approving will let the agent" summary of human-readable permission rows with an Adjust link revealing the grouped editor (Full access first, wildcard last), a centered "Finish signing in to <service> in your browser..." progress state, and a "Sign in & approve" button label when approval will run a browser sign-in.

Connector sign-in failures no longer dump a raw Node.js stack trace into the UI: the latchkey CLI's failure output is condensed to its meaningful error line (the full output still goes to the log).

The grant dialog's Adjust editor renders its permission rows as toggle switches (label left, switch right), matching the Permissions screen and the prototype; the underlying form checkboxes and the wildcard's disable-the-rest behavior are unchanged.

Reviewing a request from the Permissions pane's "Waiting on you" strip is now a detour: the popup opens stacked over the workspace-options panel and closing it restores the panel (re-rendered after a resolution, so the strip drops the resolved row) instead of dropping the user back on the workspace. Browser mode honors a return_to path the same way.

Resolution messages to the agent now retry with backoff (about two minutes) when the agent is stopped or mid-restart, instead of being silently lost -- previously a resolution that raced the agent's lifecycle left the chat card stuck on "Review & respond" and the agent never resuming. The retry aborts promptly on app shutdown.

User-visible copy across the desktop client now uses em dashes instead of double hyphens.
