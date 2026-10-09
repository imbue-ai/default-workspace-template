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
address once the workspace has been shared, else one that opens only from
inside the workspace. Give the user that link, with the page's path added; do
not build it from `apps.toml` by hand.
