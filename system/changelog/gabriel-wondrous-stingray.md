Fixed a family of app-name bugs found while updating a workspace to minds-v0.6.2.

The reserved-name set is carried in three places, because two of them cannot import the one that owns it: `forward_port.py` is stdlib-only by contract, and `layout.py` is an agent-facing script. `layout.py`'s copy had drifted to two of the eight names, so a bare word like `share` or `host` waited five seconds for a registration that could never happen instead of being refused at once. The drift test that was supposed to catch this sampled a fixed list of names rather than comparing the sets, so it could not see the gap; it now compares them.

`github` is now reserved. Enabling GitHub sync writes a `github-sync` supervisord program, and an app named `github` would have claimed it as a sidecar -- so turning GitHub sync on would have failed the manifest suite in that workspace.

Apps can no longer collide with each other's programs: an app named `pr` would have claimed a second app's `pr-review` program as its own sidecar. The existing guard only covered collisions with programs that belong to no app.

`layout.py`'s tests now run without the ambient agent identity. They set `MNGR_AGENT_ID` but read `MINDS_CHAT_ID` first, and every Minds chat agent runs with both set -- so they passed in CI and failed for every agent that ran them.
