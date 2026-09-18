The gen-2 fleet's operator WireGuard peers can now be synced for a whole tier in one run, with no env activation.

- `minds-admin wireguard sync-peers` and `wireguard config` take `--tier <dev|ci|staging|production>`, which selects the tier's committed `[management_plane]` operator list, its SSH CA, and its box registry; without the flag the activated env's tier is used as before. The wireguard commands read the fleet from the tier's `secrets/minds/<tier>/neon/DATABASE_URL` pool DB (the shared tiers' single pool DB; for dev and ci the standing "infra" registry) instead of the activated env's per-env database, so a developer's key lands on every box of the tier at once. `sync-peers` now syncs the boxes in parallel (`--max-concurrency`, default 8) instead of one after another.

- `minds-admin env deploy` imports the tier registry's `ready` bare-metal box rows into a dynamic env's `host_pool` DB right after migrating it (a no-op while the tier's `neon/DATABASE_URL` leaf is empty), so a fresh dev env can lease slices on the tier's boxes without a manual `server import-boxes`.

- The registry import moved into a shared `slices/box_registry.py` module used by both `server import-boxes` and the deploy.
