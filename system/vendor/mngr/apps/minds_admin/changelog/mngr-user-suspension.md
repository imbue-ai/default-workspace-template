New operator commands for reversible account suspension (issue #550):

- `minds-admin account suspend <email> --reason "..." [--block-storage]` -- block sign-in, revoke sessions, force-stop workspaces, block LiteLLM keys, flip R2 keys read-only (or fully disable them with `--block-storage`), and pause shares. Idempotent and re-runnable; prints a per-step report and exits non-zero on a partial run.

- `minds-admin account unsuspend <email>` -- lift the suspension: restore sign-in, unblock keys, restore R2 access per the quota state, and reactivate shares. Workspaces stay stopped until the user starts them.

- `minds-admin account revoke-sessions <email>` -- standalone session revocation (the incident-response button); state-modifying requests with a revoked token are refused within one round-trip.

- `minds-admin workspaces stop <host-db-id>` -- operator force-stop of one workspace (same data-preserving transition as the owner's stop, without the ownership check).

- `minds-admin account show <email>` now also displays the account's suspension state (`suspended_at` / `suspended_reason`).
