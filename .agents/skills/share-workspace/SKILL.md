---
name: share-workspace
description: Use when the user asks about sharing this workspace with someone else, or asks whether/how sharing works.
---

# Share workspace

Sharing is machine-level: it grants another person network access to this
workspace host through a signed relay, gated by the grants document. It is
**enabled from the desktop client, not from inside chat** -- there is no
in-workspace command that turns it on. Point the user to the sharing control
in the minds desktop app; do not attempt to enable it yourself.

## 1. Answering "how does sharing work" / "can I share this"

Explain plainly: sharing gives the other person access to this whole
workspace (not a single document or app) via a share link/grant they open in
the desktop client, and it can be turned off again from the same place. If
they ask for detail on the mechanism, it is one share per workspace host,
covering the workspace plus optional per-service scopes -- but lead with the
plain-language version unless they want the mechanics.

## 2. Detecting whether sharing is actually active

Don't rely on the user's account of whether they've shared before -- check
directly, the same way the latchkey skill checks permissions live rather
than trusting a cached flag. `data/.secrets/share.env` and
`data/.secrets/share_grants.toml` exist only while sharing is enabled (the
desktop client injects them into the workspace when a share is turned on,
and removes them when it's turned off). Their presence is the live signal:

```bash
test -f data/.secrets/share.env && echo "sharing is currently active"
```

## 3. Recording it in user knowledge

If sharing is (or becomes, during this conversation) active per step 2,
update `data/.state/user_knowledge.toml` (gitignored machine state -- see
`data/.state/README.md`) so the agent knows this user has already used
sharing and doesn't keep proactively suggesting it. Create the file if it
doesn't exist; otherwise set `has_shared_workspace = true` under `[sharing]`
without touching any other keys or tables already there:

```toml
[sharing]
has_shared_workspace = true
```

Do this once you observe `data/.secrets/share.env` present -- either because
the user just enabled sharing while talking with you, or because you checked
and found it already on. Never set this to `true` on the user's say-so
alone; always confirm via step 2 first.
