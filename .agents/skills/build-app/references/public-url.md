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
address once the workspace has been shared, else the app's port-less
`http://<label>.localhost/` address. That is the address to give the user, and
the one to link in a chat reply (with the page's path added): inside the
workspace either opens the app's own window, and the share address also opens
for anyone the workspace is shared with. Neither carries a port, so neither
goes stale when the app's port changes.

- The share domain is owned server-side and reaches the workspace only when the
  user shares it; the share gateway then keeps it in `data/.state/share_domain`,
  even after sharing is turned off (a re-share keeps the domain).
- A workspace never shared has no public address: its `<label>.localhost`
  links open only from inside the workspace. (From inside the container a
  service is still reached at its registered `http://localhost:<port>/`
  backend URL; that is for scripts, not for links.)
- Do not build the address from `apps.toml` by hand; `list` does it.
