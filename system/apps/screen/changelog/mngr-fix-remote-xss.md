The Screen reads `X-Imbue-Identity` as every app does (`workspace_layout.identity`): a present header that does not parse is refused with 400 rather than read as the owner's, and a session's display name is the email's local part, else Owner or Guest (the `name` an older proxy stamped is no longer read).

The service installs the request guard ahead of its routes (`app_schema.request_guard`): a request the browser on the Screen marks as a page of another origin's is refused with 403 whatever it carries, and a header-less request counts as the owner's only from loopback under a loopback `Host`, so a page shown on the Screen's own browser can no longer drive the owner-only API (`/api/run`, the agent verbs, the screenshot, the browser fleet) over `127.0.0.1:8090` (imbue-ai/mngr-internal#2816). Agents' clients and the shell are unaffected.

Chromium's launch arguments are checked never to disable its Local Network Access checks, the browser-side layer in front of the services' own check.
