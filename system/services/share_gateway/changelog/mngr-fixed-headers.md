Shared workspaces now hand every backend a trustworthy identity for the request:

- The share gateway sets `X-Share-Owner` (`true`/`false`) on every request, plus `X-Share-Email` carrying the visitor's verified email whenever they are not the owner. caddy strips any client-supplied copy of these headers before authorization and re-injects only the values verified by `/_auth/verify`, so a request can never spoof ownership or an email.

- The owner's email is never sent per-request. Services that need it read `data/.state/share/owner_email`, a file the minds app writes while the workspace is shared and removes when unshared (so its presence also signals that sharing is active).
