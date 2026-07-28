# The shared (Cloudflare) URL

Every registered service owns its own browser origin. Locally that is
`http://<name>.<workspace-host>/` (e.g.
`http://news.agent-ab12.localhost:8421/`). When the workspace is shared
via Cloudflare, the same service is also reachable at its own public
origin:

```
https://<name>--<host>--<user>.<domain>/
```

(the same two coordinates as the local hostname, spelled as a single
flat label with `--` separators). Every registered service is exposed
when the workspace is shared -- there is no per-service opt-in flag; a
service registered while the workspace is shared gets its DNS record,
ingress rule, and access policy automatically.

## Who can reach it (two-tier grants)

Sharing is two-tier:

- **Per-service email list**: adding an email to a service's list
  grants access to that service only.
- **Workspace master list** (the `system-interface` list): adding an
  email there grants access to *every* service in the workspace,
  including services registered later.

Grants are managed from the desktop client's workspace settings, not
from inside the container.

## Caveats when hunting for the URL from inside the container

- **The public hostname is owned server-side**, not by the
  cloudflared process running in this container. Skimming the
  `cloudflared` service's logs will not surface a URL.
- **The public URL is *not* written into `data/.state/apps.toml`.**
  `forward_port.py` only stores `name` and `url` (the local
  `http://localhost:<port>` backend address). Do not grep that file
  for a public URL.

The reliable way to get the public URL is through the desktop client
itself: the tab's origin is derived from the service name and the
workspace host. If you need the exact URL for testing, ask the user to
read it from their browser's address bar.

If the workspace is not shared, this section does not apply -- the
service is reachable only at its local origin (and, from inside the
container, at its registered `http://127.0.0.1:<port>/` backend URL).
