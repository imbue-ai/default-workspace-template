# The shared (public) URL

Every registered service owns its own browser origin, named by its label (the
`<name>-<random>` label `forward_port.py` gave it, in `data/.state/apps.toml`).
Locally that is `http://<label>.<workspace-host>/`. When the workspace has been
shared, the same service is also reachable at its own public origin, by the
SAME rule on the share domain:

```
https://<label>.<share-domain>/
```

## Getting it

`uv run --no-sync workspace-layout list` prints each app's `link`: this
address once the workspace has been shared, else the app's local
`http://localhost:<port>/` backend URL. That is the address to give the user,
and the one to link in a chat reply: anyone the workspace is shared with can
open it, and inside the workspace it opens the app's own window.

- The share domain is owned server-side and reaches the workspace only when the
  user shares it; the share gateway then keeps it in `data/.state/share_domain`,
  even after sharing is turned off (a re-share keeps the domain).
- A workspace never shared has no public address: its apps are reachable only
  at their local origins (and, from inside the container, at their registered
  `http://localhost:<port>/` backend URLs).
- Do not build the address from `apps.toml` by hand; `list` does it.
