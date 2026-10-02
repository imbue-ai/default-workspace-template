The chat reaches the shell through the shared `workspace_layout` library's client instead of its own: the auto-open reactor, the focus-chat route, and the client-activity reports behave as before.

- A send whose client or desktop id the shell would refuse is no longer reported to the shell (a warning is logged instead of the shell refusing the body).

- The auto-open reactor logs a warning, rather than an info line, when the shell answers its show of a chat with something other than a show's answer; the chat stays held and is retried as before.

- The focus-chat route answers 400 for a client id the shell would refuse, instead of passing it on.

- The paths the chat shows (its root with a chat selected, a chat's own page) are typed window paths and pages, checked before they are sent to the shell.

- Frontend comments name the `workspace-layout` command in place of the removed `system/scripts/layout.py`.
