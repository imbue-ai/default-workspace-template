New `just audit-boxes` recipe: SSHes every bare-metal box for the activated minds env and reports its real occupancy (all envs' slices, not just this env's rows) plus any cross-tier contamination -- a slice stamped for another tier, or an extra key in the lima user's `authorized_keys`. Read-only; the pool SSH key is resolved from the activated tier's Vault entry.

`just list-servers` grew a comment making its limitation explicit: its slot columns come from this env's own `pool_hosts` rows, so a box shared with another env reads as emptier than it actually is. Use `just audit-boxes` before concluding you have free slots.

The `minds-justfile` skill doc was updated to match, so the recipe index agents read lists `just audit-boxes` and carries the same caveat on `just list-servers`.
