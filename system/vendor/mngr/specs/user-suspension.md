# User-account suspension (minds / imbue-cloud)

Reversible operator suspension of a user account: block login, revoke sessions, freeze
compute, and cut off every non-session credential the account holds -- all
data-preserving and restorable by a single unsuspend. Covers issue #550's D2 (operator
session revocation) and D3 (immediate revocation on state-modifying routes) as the
foundation.

## Overview

- The 2026-08 SSH-storm incident showed we cannot lock out a compromised/abusive
  account: no operator session revocation exists, revocation is not immediate (stateless
  ~1h access tokens), and nothing stops the account's workspaces, LiteLLM keys, R2
  tokens, or relay tunnels.
- Enforcement model: "no valid session, and no way to get one". A persistent suspension
  flag gates every session-creation/refresh path; suspend revokes all existing sessions
  (D2); state-modifying routes verify sessions against the SuperTokens core per request
  (D3, `check_database=True`), so revocation bites within one request. Resource routes
  need no separate suspension check.
- Suspension is orthogonal to plans and entitlements: a `suspended_at` /
  `suspended_reason` column pair on `account_entitlements`, never a plan switch, so
  unsuspend restores the account exactly (including operator-granted quota bumps).
- Non-session credentials get an explicit, reversible fan-out: force-stop running
  workspaces (existing stop transition; severs SSH, preserves data, frees the slot),
  block LiteLLM keys per-key, flip R2 keys read-only (or fully disable with
  `--block-storage`), and suspend shares by state so live relay tunnels die within ~10s
  via a new `Ping` plugin subscription.
- Operator surface: admin-key connector endpoints (`suspend`, `unsuspend`,
  `revoke-sessions`, per-workspace force-stop) plus matching `minds-admin account` /
  `minds-admin workspaces` commands. The suspend fan-out is server-side, idempotent, and
  re-runnable with a per-step report.

## Expected behavior

Operator actions:

- `minds-admin account suspend <email> --reason "..."` (reason required, no extra
  confirmation prompt) sets the flag, revokes all SuperTokens sessions, force-stops
  every running workspace, blocks all LiteLLM keys, flips R2 keys read-only, and
  suspends all shares. It prints a per-step report; re-running converges after a
  partial failure.
- `--block-storage` additionally disables the account's Cloudflare R2 tokens outright
  (no reads). Re-running `suspend` with the flag escalates an existing suspension;
  re-running without it never silently de-escalates. De-escalation = unsuspend +
  re-suspend.
- `minds-admin account unsuspend <email>` clears the flag, unblocks LiteLLM keys,
  restores R2 keys to whatever the storage-quota state dictates, and reactivates
  shares. Workspaces stay stopped until the user starts them; the user signs in fresh.
- `minds-admin account revoke-sessions <email>` (D2) revokes all sessions standalone --
  no SuperTokens core credentials needed, ever.
- `minds-admin workspaces stop <host-db-id>` force-stops one workspace (general-purpose:
  suspension, migration, future idle shutdown).
- `minds-admin account show` and `GET /admin/accounts/{email}` display suspension state.

Suspended user's experience:

- Sign-in (browser page, JSON API, Google OAuth, device handoff) fails with a
  structured `account_suspended` refusal; the hosted login page, the desktop client,
  and the `mngr imbue_cloud` CLI all show a generic "account suspended -- contact
  support@imbue.com" message. The internal reason is never shown.
- Session refresh fails (refresh tokens revoked; the refresh path also checks the
  flag). Held access tokens stop working on any state-modifying request immediately
  (D3); read-only requests drain out over the token's remaining ~1h lifetime
  (accepted).
- Running workspaces halt (SSH severed, processes killed) and upload as encrypted
  artifacts; no data is deleted anywhere.
- LiteLLM virtual keys are rejected by the proxy; keys cannot be re-minted (no
  session).
- R2 credentials go read-only by default (backups remain retrievable), or fully dead
  under `--block-storage`; the same credentials come back on restore (in-place policy /
  status flips, verified against the Cloudflare token API).
- Shared workspaces stop serving within ~10s -- including shares from the user's own
  local machine -- and visitors get nothing from the relay.
- Password reset still works (grants no access; sign-in stays blocked). No suspension
  email is sent in v1. Re-signup with a new email is out of scope (signup IP hardening
  covers it).

D2 / D3 behavior changes independent of suspension:

- Operators can revoke any account's sessions by email via connector + CLI.
- After any revocation, the very next state-modifying request with a revoked-but-
  unexpired access token gets a 401. Read routes keep cheap stateless validation with
  no added DB cost.
- A normal user unshare now also severs the live tunnel within ~10s (today it lingers
  until reconnect) -- the Ping rejection rule is "token does not resolve to an active
  share".

Robustness properties of the tunnel kill:

- The workspace frpc already sends 10s application-layer heartbeats; a rejected Ping
  makes frpc close its session and re-login, which the connector refuses for non-active
  shares. Recovery after any drop is frpc's existing redial loop -- automatic, no user
  action.
- The connector's Ping handling fails open on its own internal errors (allow + warning
  metric) and rejects only on an affirmative non-active verdict; Login/NewProxy stay
  fail-closed. Tunnel uptime is therefore coupled only to the connector's HTTP frontier
  being reachable; a connector outage drops tunnels for its duration and they self-heal.

## Changes

Ordered as independently shippable slices.

Slice 1 -- D2 + D3 (small; closes the incident gaps immediately):

- Connector: `POST /admin/accounts/{email}/revoke-sessions` (admin-key auth) resolving
  email -> user id -> `revoke_all_sessions_for_user`.
- Connector: `resolve_web_user_identity`'s Bearer branch passes `check_database=True`
  for non-GET/HEAD/OPTIONS methods (mirroring its existing cross-site gate); an
  override parameter exists for future sensitive GETs. `POST /account/plan` migrates
  from raw `authenticate_request` onto this helper.
- `mngr_imbue_cloud` connector client: `admin_revoke_sessions` method.
- `minds-admin account revoke-sessions` command.

Slice 2 -- suspension flag + login gates:

- Migration: `suspended_at TIMESTAMPTZ` + `suspended_reason TEXT` on
  `account_entitlements`.
- Connector: a suspension check (structured 403 `account_suspended` / browser status
  `ACCOUNT_SUSPENDED`) at every session-creation/refresh path: `/accounts/api/signin`,
  `/accounts/api/signup` (defensive), the Google OAuth callback, deprecated JSON
  `/auth/signin`, `/auth/session/refresh`, and `POST /auth/device/token`.
- Connector: `POST /admin/accounts/{email}/suspend` (body: required reason, optional
  block_storage) and `POST /admin/accounts/{email}/unsuspend` -- at this slice they set/
  clear the flag and revoke sessions; the fan-out steps land in slice 3 behind the same
  endpoint and report shape.
- Suspension state surfaced in `GET /admin/accounts/{email}` (additive wire field).
- `minds-admin account suspend / unsuspend` commands; `account show` renders the state.
- Hosted login page (connector frontend bundle): render `ACCOUNT_SUSPENDED` as the
  generic message + support@imbue.com.

Slice 3 -- credential/workspace fan-out + tunnel kill:

- Connector suspend fan-out (idempotent steps, per-step report): CAS every `leased`
  workspace row to `stopping` + spawn supervisors (rows in `starting` are reported and
  caught by a re-run once leased); block each LiteLLM key via `/key/list` +
  `/key/block` (unblock on unsuspend); flip R2 tokens read-only in place recording a
  suspension enforcement state distinct from the quota sweep's (sweep and roll-key must
  not undo it), or disable token status under block_storage; set all shares to a new
  `suspended` state keeping relay-token rows.
- Unsuspend fan-out: unblock keys, restore R2 access per current quota enforcement,
  shares back to `active` (tunnels resume when the workspace starts -- no re-share, no
  re-injection).
- Connector: `POST /admin/workspaces/{host_db_id}/stop` (admin-key), the standalone
  force-stop route.
- Connector `frps_auth`: handle the `Ping` op -- reject when the token does not resolve
  to an active share, fail open (allow + warning metric) on internal errors. Also check
  share state on `POST /shares/cert`.
- `share_relay` config render: add `"Ping"` to the subscribed plugin ops. Deploy
  ordering: connector first, then relay redeploys (an un-redeployed relay just keeps
  today's reconnect-only enforcement). No default-workspace-template changes (10s
  heartbeats already configured).
- `minds-admin workspaces stop` command.
- Metrics via existing conventions: suspend/unsuspend operations, blocked sign-in
  attempts, Ping rejections, Ping fail-open occurrences.

Slice 4 -- tests + clients:

- Deployment test in `apps/minds/deployment_tests/` (operator-invoked against live ci
  tiers, never per-branch): provision a verified user holding a live session, suspend,
  assert sign-in is blocked with the structured status / the held session gets a 401 on
  a state-modifying call within one request / the admin view reports the suspension,
  unsuspend, assert sign-in and a fresh session work again. The credential fan-out
  details (LiteLLM key blocking, R2 token flips, workspace force-stop, share
  suspension) are unit-tested in the connector rather than re-proven here; tunnel-kill
  logic is covered by unit/integration tests plus one manual staging verification, not
  by this test.
- Unit/integration tests throughout the slices per the existing per-module patterns
  (fake SuperTokens backend, fake stores, config-render snapshots).
- Client messages: `mngr imbue_cloud` CLI and desktop sign-in paths map
  `account_suspended` / `ACCOUNT_SUSPENDED` to the human message with
  support@imbue.com.

Non-goals:

- No account deletion changes (`scripts/delete_accounts.py` remains the terminal path).
- No re-signup prevention (signup IP hardening owns that).
- No suspension notification email, no suspension-events history table, no read-route
  DB checks, no relay-bounce/kick infrastructure beyond the Ping subscription.
