The app shell HTML is now served with `Cache-Control: no-store`, so reloading the interface always lands on the build that is currently on disk.

The shell is assembled per request and the assets it links are content-hashed, which makes its freshness the only thing deciding which bundle a reloaded page runs: a cached shell names the old bundle forever. A page cannot drop its own HTTP cache -- the `location.reload(true)` form is a Firefox-only extension -- so `reloadInterface` can only reload and trust the response to be fresh, and `no-store` is what makes that trust well-founded. This matters most for anyone viewing the workspace through a shared Cloudflare tunnel, where an intermediary is free to cache whatever is not marked otherwise.

Together with the new `system/scripts/refresh_workspace_view.py` helper, this is what makes a reveal of a changed interface actually visible to whoever is looking at it, instead of leaving them on the previous build until they navigate away and back.
