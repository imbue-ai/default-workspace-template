Reversible account suspension (issue #550):

- New admin-key endpoints: `POST /admin/accounts/{email}/suspend` (required reason, optional `block_storage`), `POST /admin/accounts/{email}/unsuspend`, `POST /admin/accounts/{email}/revoke-sessions`, and `POST /admin/workspaces/{host_db_id}/stop` (operator force-stop, no ownership check).

- Suspend is an idempotent, re-runnable server-side fan-out with a per-step report: sets the `suspended_at`/`suspended_reason` flag on `account_entitlements` (migration 030), revokes every SuperTokens session, force-stops all leased workspaces via the existing stop transition, blocks each LiteLLM key (`/key/block`), flips R2 tokens read-only in place (or disables them outright under `block_storage`, recorded in `r2_keys.suspension_access` so the quota sweep never undoes it), and suspends all shares by state while keeping relay tokens (unsuspension is self-healing, no re-share needed).

- Every session-creation/refresh path (browser sign-in/sign-up, Google OAuth callback, JSON `/auth/signin`, `/auth/session/refresh`, the device-code exchange) now refuses suspended accounts with a structured `ACCOUNT_SUSPENDED` status / 403 `code: account_suspended`; the hosted login page renders the generic suspended message with the support contact.

- State-modifying routes now verify Bearer sessions against the SuperTokens core per request (`check_database`), so a revoked session is refused within one request instead of coasting on its ~1h access token; read routes keep stateless validation. `POST /account/plan` moved onto the shared web-identity helper (gaining browser-cookie support).

- The frps auth callback now handles the `Ping` op: a heartbeat whose relay token does not resolve to an active share is rejected, severing LIVE tunnels of suspended (and freshly unshared) workspaces within ~10s. Ping handling fails open on connector-internal errors so tunnel uptime is coupled only to the connector being reachable.

- The R2 storage-quota sweep skips suspended owners (and any key under suspension enforcement).
