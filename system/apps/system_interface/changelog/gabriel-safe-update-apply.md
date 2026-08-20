The system interface hardens against update-time version skew.

Its single mngr `load_config` call site now parses with `strict=False`, so a `.mngr/settings.toml` written for a newer mngr degrades to a logged warning instead of a 500 on every agent listing and message send (the lockout that made a broken update self-locking). `mngr config set` and the CLI keep strict parsing.

The server records the tree HEAD it started from and stamps an `X-Workspace-Update-Staleness` header (and meta tag) on app-shell responses when the live tree has moved under it in a way that affects what this process runs (backend code, its dependency manifests, the vendored mngr, `.mngr/settings.toml` -- never ordinary agent commits, docs, or the frontend, whose bundle is rebuilt without a restart) -- `updated-not-activated`, or `update-interrupted` when the update apply's marker is present -- and a small informational banner renders the two messages, pointing the user at their agent.

The reveal machinery that lived in `reveal_system_interface.py` moved into the general update apply (`update_self.py apply`); the script keeps only the pre-merge `preview`/`unpreview` adapters, and going live is now one atomic merge-and-reveal with no agent-prose pause between landing and activation.
