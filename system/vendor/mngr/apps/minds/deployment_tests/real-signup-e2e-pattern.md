# Pattern: real-signup end-to-end tests (the swarm template)

This is the recipe a hand-written account-signup e2e test follows, so a
behavioral swarm (`tmr-behaviors`) can replicate it as a template instead of
re-deriving it per agent. It is written against the concrete anchor tests in
`apps/minds/deployment_tests/test_account_signup_e2e.py`; read that file
alongside this doc -- the doc explains *why*, the file shows *how*.

The distinguishing property of these tests: they drive the **real** signup
surface (`POST /accounts/api/signup` on the live connector), not the admin
bypass (`POST /admin/test-signup`) that `verified_user` / `sync_e2e_account`
use. That admin shortcut exists precisely so cheap tests do not re-run the
realistic flow (`test_logged_in_smoke.py:1-9`); a template for the *signup*
surface must not take it.

## When to use this pattern vs. the admin bypass

| You are testing... | Use |
|---|---|
| Anything downstream of "a user exists" (shares, quotas, plan, litellm) | `verified_user` (admin bypass) -- do not pay the signup cost |
| The signup surface itself: account creation, verification email, the device handoff gates, the hosted pages | this pattern (real `POST /accounts/api/signup`) |

## Anatomy of a real-signup test

1. **Marks.** `pytestmark = [pytest.mark.release, pytest.mark.minds_services]`.
   - `release` puts the test in the release suite (discovered by tag, not path;
     it never lands on a runner without a ci env -- the mngr release workflow
     excludes `apps/minds` by path).
   - `minds_services` routes it to the pre-stood-up shared ci env tier
     (connector + litellm + SuperTokens). Use this, not `minds_deployment`:
     signup needs a live connector + SuperTokens, not a fresh-minted env, so it
     belongs on the cheap shared tier. `minds_deployment` is only for tests of
     the deploy/rollback process itself.
   - Both marks are excluded from `test-offload` and `just test-quick`, so these
     never run on a normal push. They run on the opt-in dispatch
     (`gh workflow run ci.yml -f run_minds_release_tests=true`) or the local
     `just minds-test-*` recipes.

2. **Per-test timeout.** `@pytest.mark.timeout(...)`. The pyproject-wide default
   (10s) is sized for in-process unit tests. A live-env test must override it to
   cover `wait_for_env_ready`'s cold-boot poll (up to ~120s) plus its own work.
   Anything that waits on real email delivery needs a much wider budget (the
   verification test uses 360s, with a 180s inner wait for the email itself).

3. **Env handle + readiness.** Take the `shared_env` fixture and call it for the
   `default` role, then `wait_for_env_ready(env)` as the very first line of the
   body. This tolerates cold-boot / container-swap windows so they surface as a
   wait, not a flake. Read URLs off `env.urls`; read per-env SuperTokens/Neon
   secrets off the `SharedEnvHandle` (`env.supertokens_connection_uri`, etc.).

4. **Create the account for real.** `POST {connector}/accounts/api/signup` with
   `{"email", "password", "turnstile_token": ""}`. Turnstile is disabled on
   ci/dev tiers (no `TURNSTILE_SECRET_KEY`), so the empty token is accepted; a
   configured tier would refuse it, and a test there would need a seeded
   account instead. The password must satisfy the SuperTokens default policy
   (>=8 chars, at least one letter and one digit) -- `signup_field_rejection`
   runs the SDK's validators server-side even though the SDK's own HTTP routes
   are disabled. The response body's `user.user_id` is what you register for
   cleanup (see below). The signup establishes a **cookie session** in the
   `httpx.Client`'s jar; subsequent same-client calls are authenticated as that
   browser session.

5. **Clean up the account you created.** Take `register_signup_user_for_cleanup`
   and call it with each `user_id` you create. This deletes the user via the
   `default` env's SuperTokens admin API on teardown. It matters because the
   session-scoped stale-user sweep only deletes `test-<hex>@example.test`
   addresses; a realistic address (especially a mail.tm `+<uuid>` one) does not
   match, so without explicit cleanup those accounts leak on the shared env
   across runs. Even swept addresses should be registered -- deterministic
   cleanup beats leaning on the 30-minute sweep.

## The mail.tm seam (real verification-email click-through)

This is the seam nothing in `deployment_tests/` used before
`test_realistic_signup_verifies_email_via_mailtm`. Use it to witness the real
verification loop:

- Take the `signup_email` fixture. It hands you a `MailtmInbox` rooted at a
  fresh `<runner-account-local>+<uuid>@<mailtm-domain>` address. Sign up with
  `str(signup_email.address)` as the email. The `+<uuid>` suffix isolates your
  inbox from other concurrent tests sharing the per-run mail.tm account.
- The fixture **skips cleanly** when the orchestrator did not provision a
  mail.tm account (`MAILTM_ACCOUNT_ADDRESS` / `MAILTM_ACCOUNT_JWT` unset), so a
  stray `pytest` outside the orchestrator does not hard-fail.
- Trigger the email with `POST /accounts/api/send-verification` (needs the
  cookie session from signup). The connector sends via SuperTokens' default
  (backward-compatibility) email service; a brand-new user is never on the 60s
  per-user cooldown, so `{"status":"OK","sent":true,"already_verified":false}`.
- `signup_email.wait_for_verification_token(timeout_seconds=...)` polls the
  mailbox and extracts the token from the emailed link. Give it a generous
  timeout: real delivery through a hosted email service is the slow part.
- Consume the token at `POST /accounts/api/verify-email`, then re-read
  `GET /accounts/api/me` and assert `email_verified` flipped `false -> true`.

The delivery is best-effort, so the test is `flaky` and resends. The connector
sends through SuperTokens' hosted service (`api.supertokens.io`), and the SDK
**swallows send failures** (`except Exception: pass`) -- so `send-verification`
returns `sent:true` even when nothing left, and a single send can silently not
deliver. A live A/B probe (send from the connector's Modal workspace vs. local)
showed delivery is reliable in isolation, so misses are rare, but not zero.
Two things follow, and they are the reusable lesson for any real-email witness:
(a) mark the test `@pytest.mark.flaky`; (b) on a missed poll, re-`POST
send-verification` (after the 60s cooldown, so the resend actually dispatches)
and poll again, self-healing a single dropped send. A sustained hosted-email
outage still fails -- that is the honest cost of witnessing the real send/deliver
path instead of tolerating its absence. (Making the swallowed failure *visible*
would mean the connector supplying its own email-delivery `service` that raises,
a production-UX change outside the test's scope.)

Gotcha fixed while writing this template: `MailtmInbox.wait_for_verification_token`
filtered inbox subjects on the substring `"verify"`, but the connector's
verification email is subject-lined "Email verification instructions", which
contains "verification" but not "verify" -- so the never-exercised seam would
have silently timed out. The filter now matches the `"verif"` prefix (shared by
both spellings, still excluding the one-time sign-in email keyed on `"sign"`).
When adding a template that relies on an unused seam, budget for exactly this
kind of latent break and prefer a live run to confirm it.

## How secrets / env reach the test

You never read Vault or Modal directly from a test body. The orchestrator
(`apps/minds_admin/scripts/test_deployments.py`) and the conftest fixtures do it
for you:

- `MINDS_DEPLOYMENT_TEST_ENVS_JSON` points at a per-run JSON file of shared-env
  URLs (`deployment_envs_config` loads it; every fixture skips with a clear
  reason if it is unset).
- Per-env secrets arrive as `MINDS_DEPLOYMENT_TEST_SHARED_<ROLE>_<KEY>` env vars
  (the only path that works inside an offload sandbox); local runs fall back to
  reading the env-keyed Vault path. `shared_env` resolves whichever is present,
  so the same test body runs in CI and locally.
- The mail.tm account (`MAILTM_ACCOUNT_ADDRESS` / `MAILTM_ACCOUNT_JWT`) is
  created fresh per run and torn down at the end. Locally the `run` /
  `services-against` orchestrator flows mint it inline; in CI the opt-in
  `run_minds_release_tests` dispatch mints it via the orchestrator's
  `mailtm-up` / `mailtm-down` subcommands and exports the two vars into the
  `minds_services` pytest step (JWT masked). Absent those vars, `signup_email`
  skips -- so the verification test runs only where a mailbox was provisioned.

## Running it

Requires `vault login` + a `minds-dev` Modal profile
(`~/.modal.toml [minds-dev]`). From the repo root
(`apps/minds/deployment_tests/README.md` has the full menu):

```bash
# Point the services tests at an already-deployed dev env (no env create/destroy).
# This also mints a per-run mail.tm account, so the verification test can run:
just minds-test-services-against dev-<you> apps/minds/deployment_tests/test_account_signup_e2e.py

# ...or stand up a fresh shared ci env, run, tear down (real cloud spend):
just minds-test-deployment-up default
#   copy/run the printed `MINDS_DEPLOYMENT_TEST_ENVS_JSON=... pytest -m minds_services ...`
just minds-test-deployment-down
```

Outside the orchestrator these tests **skip** (they never fail-hard on missing
config), so a plain `uv run pytest apps/minds/deployment_tests/` is a safe
collection/skip smoke check but does not exercise anything.

## For the behavioral swarm

- One `.feature` unit per observable property; witness it with a test that
  follows the anatomy above. Keep `witnesses("<coordinate>")` markers pointing
  at the connector-accounts corpus units (a corpus for
  `apps/remote_service_connector` does not exist yet -- see the MIND-195
  research report, `specs/account-creation-signin/research.md` on the plan
  branch, section 4.2, for the one-corpus-vs-two decision the swarm inherits).
- Prefer the HTTP wire contract (status codes, redirect targets, JSON shapes)
  over browser-rendering assertions; reach for Playwright
  (`pytest.importorskip("playwright.sync_api")`, skip when chromium is absent)
  only for genuinely frontend-only properties like the Google-vs-email layout.
- If a property needs a seam the shared env cannot stand up (real Google OAuth,
  Turnstile enabled, the marketing cookie), record it as an `infra_blocker`
  rather than faking it -- that discipline is what keeps the corpus honest.
