# The shared (public) URL

Every registered service owns its own browser origin. Locally that is
`http://<name>.<workspace-host>/` (e.g.
`http://news.host-ab12.localhost:8421/`). When the workspace is shared,
the same service is also reachable at its own public origin, by the
SAME rule on the share hostname:

```
https://<name>.<workspace-share-host>/
```

The bare share hostname is the workspace shell, and each registered
service prefixes its name as one more hostname label -- exactly like
the local origin, just with a longer base.

## Caveats when hunting for the URL from inside the container

- **The public hostname is owned server-side**, not by anything
  running in this container. Skimming service logs will not surface a
  URL.
- **The public URL is *not* written into `data/.state/apps.toml`.**
  `forward_port.py` stores `name`, `url` (the local
  `http://localhost:<port>` backend address), the service's `label`,
  and any registered `icon` markup -- never a public URL. Do not grep
  that file for one.

The reliable way to get the public URL is through the desktop client
itself: the tab's origin is derived from the service name and the
workspace host. If you need the exact URL for testing, ask the user to
read it from their browser's address bar.

If the workspace is not shared, this section does not apply -- the
service is reachable only at its local origin (and, from inside the
container, at its registered `http://127.0.0.1:<port>/` backend URL).
