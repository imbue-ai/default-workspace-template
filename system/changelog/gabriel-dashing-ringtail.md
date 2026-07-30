Added `system/scripts/refresh_workspace_view.py`, the shared motion for rebuilding the user's view of the workspace after its interface changes -- above all after `mngr start --restart system-services`, which bounces the system interface underneath whatever the user has open.

Nothing reloaded that view before. The Minds app only intervenes when a workspace looks unreachable for a sustained stretch, and a services restart that comes back quickly never crosses that bar, so the user was left reading the page the previous build had rendered.

The helper fires three channels, because no one of them reaches every viewer.

It first records a new *view epoch* in `data/.state/view_epoch`. The system interface serves that epoch into the app shell and sends it on every WebSocket connect, so a page that reconnects carrying an older epoch reloads itself. This is what reaches a browser that was disconnected when the reveal landed -- or shut entirely -- and because it is a write to disk, it does not care whether anything is up yet. That is also why the helper does not wait for the restarted interface to answer: waiting would not have been sufficient anyway, since a browser reconnects on exponential backoff (up to 30s) and so is usually not yet listening at the moment the server starts answering again.

It then broadcasts `reload_system_interface`, which reloads every browser attached right now, including anyone the workspace was shared with over a Cloudflare tunnel. That broadcast is a live fan-out with no replay, which makes it an optimization rather than the mechanism: it reloads whoever is already connected immediately, and the epoch covers everyone else.

Finally it POSTs the Minds app's `/api/v1/agents/<primary>/refresh`, which reaches only the desktop app but drops its HTTP cache before reloading.

All three are fire-and-forget and the script always exits 0: the change has already landed on disk, so a viewer that cannot be reached is not a reason to fail a reveal.

The helper resolves the workspace's primary agent id by the `is_primary` label alone. It previously required a `workspace` label alongside it, which the Minds app stopped setting on its agents some time ago -- so the lookup matched nothing in every real workspace. When the lookup cannot run at all, the Minds app refresh is now skipped and reported, rather than aimed at the calling agent's own id: for a sub-agent that names a window the app is not showing, so the gateway accepts it, the app matches it to nothing, and it reads as a success while doing nothing.

The system-interface shell HTML is now served `Cache-Control: no-store`. It is assembled per request and names content-hashed assets, so its freshness alone decides which bundle a reloaded page runs -- and a page cannot drop its own HTTP cache, since `location.reload(true)` is a Firefox-only extension. This matters most for tunnel viewers, where an intermediary may cache anything not marked otherwise.
