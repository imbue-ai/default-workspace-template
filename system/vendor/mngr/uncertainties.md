# Uncertainties

Conflicts between documentation and code, noticed while writing specs; resolve and delete entries as they are fixed.

## minds-deployment-tests.md carries superseded deferred/open items

`specs/minds-deployment-tests.md` still lists the pool-host bake/lease/user-isolation test as "deferred to a follow-up PR" (described in OVH-VPS-era terms) and GitHub Actions CI integration as "blocked on solving vault-in-runner".
Both are stale: CI runs the deployment tests via Vault OIDC today (`build-minds-ci-env` in ci.yml), and the pool test is now specified (slice-era) by `specs/remote-workspaces-in-ci.md`.
Noticed while writing that spec; it assumes the newer state is correct.

## default-workspace-template issue #521 rests on a superseded networking premise

Issue #521 (imbue-ai/default-workspace-template) motivates a per-chat WebSocket conversion by the browser's ~6-connection HTTP/1.1 per-origin cap.
Both deployed browser paths already negotiate HTTP/2: `minds run` spawns `mngr forward --use-http2` unconditionally (`apps/minds/imbue/minds/desktop_client/forward_cli.py`), and the share gateway's Caddyfile pins `protocols h1 h2`, so the cap does not apply; the practical ceiling is hypercorn's 100 concurrent h2 streams per client connection.
Noticed while writing the (since replaced) split-chat-apart plan; its successor, default-workspace-template's `docs/system/blueprint/workspace-app-model/plan-workspace-app-model.md`, keeps SSE as the per-chat transport and treats the channel consolidation as a chat-internal cleanup.
Resolve by updating the issue when the chat app lands as its own program.
