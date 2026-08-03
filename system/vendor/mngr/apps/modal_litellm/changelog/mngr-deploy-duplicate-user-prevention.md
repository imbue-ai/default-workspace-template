Run the LiteLLM Prisma schema migration over Neon's direct (non-pooled) host and retry transient connection failures.

`migrate_db` now strips Neon's `-pooler` suffix from the `DATABASE_URL` hostname before running `prisma db push`: Prisma's schema engine takes session-scoped advisory locks, which are unsafe through PgBouncer's transaction-mode pooling. The resolved host is logged for verification.

Connection-class prisma failures (P1001/P1002/P1017 -- e.g. a transient network blip in the fresh Modal container, which caused two staging deploys to fail and auto-roll-back on 2026-07-30) are now retried with exponential backoff. Non-connection failures (auth, schema, migration state) still fail fast so the deploy's rollback fires immediately.
