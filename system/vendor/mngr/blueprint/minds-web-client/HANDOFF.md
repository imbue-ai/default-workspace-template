# Minds web client — handoff (branch `mngr/final-web-details`)

Status of the `minds-web-client` work (spec: `plan-minds-web-client.md`). This
replaces every earlier handoff on this doc. Written for whoever picks it up
next. The full browser loop (sign in → unlock/set-password → create → silent
owner entry in the iframe → backups over the exec channel → destroy) has been
driven end to end in a real browser (Chromium) against the live `dev-josh-2`
tier; the specific gaps that remain are enumerated under "Remaining work".

## Branches / repos / deployments

- **mngr monorepo**: branch `mngr/final-web-details`, **PR #320** (base
  `mngr/finish-web-only`), HEAD `2cafdbf026`. All CI checks green, mergeable.
  It is stacked: `josh/web-only` ← `mngr/finish-web-only` (**PR #319**, green)
  ← `mngr/final-web-details` (**PR #320**). Nothing is merged yet; the whole
  stack still has to land (see "Merge / release").
- **default-workspace-template (dwt)**: branch `mngr/final-web-details` on
  **imbue-ai/default-workspace-template**, HEAD `34699e76` (CI pairs the dwt
  branch to the mngr branch BY NAME, so it must stay pushed), mirrored to the
  private deployable copy **joshalbrecht/final-web-details** (local clone
  `~/project/final-web-details`; its PAT/LiteLLM `.env` step was skipped
  because stdin was not a TTY — create one by hand if that repo is ever
  deployed as a workspace template). Local working checkout:
  `.external_worktrees/default-workspace-template` (on `mngr/final-web-details`).
  `~/project/default-workspace-template` (branch `josh/finish-web-only`) is
  Josh's own clone — do not commit there. The only dwt change on this branch vs.
  `mngr/finish-web-only` is the system_interface terminal-origin fix (commit
  `34699e76`); everything else came in earlier via `fa55b53d`.
- **dev-josh-2 is deployed from this branch** (connector + LiteLLM + migration
  023): connector `https://minds-dev-dev-josh-2--rsc-dev-api.modal.run/`
  (chrome at `/web`). Redeploy with
  `eval "$(uv run minds env activate --deploy dev-josh-2)" && uv run minds env deploy`
  (note: `--deploy` activation mode is required).
- **dev1 relay repointed**: `relay.dev1.minds-dev.com`'s frps plugin-auth URL
  now targets dev-josh-2's connector (was dev-josh-1's, which rejected every
  dev-josh-2 relay token with "unknown or inactive relay token"). Redeployed via
  `just deploy-share-relay <ip> dev1 minds-dev.com "https://<connector>/frps/auth/<FRPS_AUTH_SECRET>"`
  using the Vault relay-ssh key. **This is a shared dev-tier relay, so dev-josh-1
  shares are now the ones locked out** — see "Remaining work / infrastructure".
- **Dev pool box** `15.204.140.221` (`ns1012536...`, server id
  `e1396039-1a7f-4583-b5ab-03d7ad47553d`) is registered in dev-josh-2. Its 6
  slots are shared across every dev env, so capacity oscillates near full; a
  freshly-baked available slice can be consumed at any time. `just bake-slice-dev
  US-EAST-VA "" 1 --server-id <id>` bakes one from the
  `.external_worktrees/default-workspace-template` checkout's current branch
  (which stamps `repo_branch_or_tag` — keep it on the branch matching the deploy
  pin). As of this writing one available slice is baked and unclaimed for
  dogfood; `mngr imbue_cloud admin server list` shows the truth.

## What is DONE (built + unit-tested; live-verified where stated)

All of the following landed on `mngr/final-web-details` (PR #320) unless noted.

1. **PR #319 CI made green.** Two `test-offload` failures fixed: an unsorted
   import in `generate_crypto_vectors.py` and stale generated CLI docs (plus the
   `libs/mngr` changelog entry that the docs regen then required).
2. **`POST /hosts/claim` boots the adopted workspace** (commit `4f879f6ee3`,
   live-verified). Baked slices are left stopped; claim now runs dwt's shared
   `minds_start_services_agent.sh` over SSH after the adopt and before the share
   bring-up, so a boot failure releases the lease and never leaves a share row
   for a dead workspace. Verified on a real claimed slice: tmux sessions, the
   bootstrap-created chat agent named from the claim, supervisord services, and
   owner-exec `/_alive` all came up unattended.
3. **Cookie-session auth fixed on every quota-checked endpoint** (commit
   `c919143385`, live-verified). `resolve_entitlements_for_user` re-derived the
   caller from the `Authorization` header, so the chrome's cookie-authenticated
   claim/lease/key-mint/bucket/cleanup-grant calls all 401'd "Invalid token". It
   now takes the user id the endpoint's own auth resolved (Bearer or browser
   session). Regression test: a cookie-authenticated claim.
4. **Routable entry origin for the chrome** (commits `6a26965d45`, `8eb66f4257`,
   `0263f69800`, validation/misc follow-ups, live-verified). The share stack
   routes only `<label>.<domain>` origins on the relay (the bare domain is
   unrouted, shielding it from CT scanners), but the SPA iframed/probed the bare
   domain, so no workspace could ever open at `/web`. The connector now reads the
   shell service's label from the workspace's `apps.toml` at share bring-up,
   records it on the share row (new `entry_label` column, **migration 023**,
   shape-validated on both ingest paths, COALESCE-preserved), returns it from
   claim + share status, and accepts it from client-side `POST /shares`
   (`mngr imbue_cloud shares create --entry-label`, threaded through the desktop
   share flow too). The chrome enters/probes/iframes `<entry_label>.<domain>`
   with a bare-domain fallback. Share-status reads are now cookie-capable.
   Deterministically confirmed live: the labeled origin returns 204 while bare
   service-name origins do not route at all.
5. **`SameSite=None; Secure; Partitioned` accounts session cookie** (commit
   `2cafdbf026`, live-verified). Was `SameSite=Lax`, so the broker's
   `/share/authorize` leg — which runs inside the cross-site chrome iframe —
   could not see the session, and the owner was wrongly bounced through the
   "Choose an account" interstitial instead of entering silently. Now the cookie
   rides the iframe via CHIPS (same top-level partition — the connector origin —
   where it is set at `/web` login). CSRF is unchanged and does NOT rely on
   SameSite: every state-changing route already refuses a cross-site `Origin`
   (`_reject_cross_site_post`) and every session read runs with
   `anti_csrf_check=False`, so `anti_csrf="NONE"` (the SDK would otherwise force
   `VIA_CUSTOM_HEADER`, which the non-SDK frontends cannot satisfy, breaking
   refresh). A new `PartitionedCookieMiddleware` appends the `Partitioned`
   attribute the SDK cannot emit. **Operational note: every existing signed-in
   session on any tier must sign in once more to obtain the partitioned cookie.**
   Verified live: both `sAccessToken`/`sRefreshToken` come back
   `SameSite=None; Secure; Partitioned`, and a fresh-login browser create entered
   the workspace iframe silently (no interstitial).
6. **Desktop "Enable web access" create toggle** (commit `97733f5bcb`,
   default off; unit-tested only). Threads `enable_web_access` through
   `CreateWorkspaceRequest` into a post-create `WebAccessEnabler`: imbue_cloud
   rows call the connector enable-sharing primitive, local docker/lima rows run
   the desktop share flow with the owner as sole grantee (and now record the
   shell entry label so those local shares are reachable from `/web` too).
   Requires a selected account (400 otherwise). Share bring-up failure never
   flips a successful create.
7. **Desktop imbue_cloud share-enable delegates to the connector primitive**
   (commit `7d8a727de9`, unit-tested only). An unshared cloud row's full
   provisioning now uses the connector enable-sharing path (same as web creates),
   then overwrites the owner-only grants seed with the user's grants document.
   Desktop `share.env` now carries `SHARE_CHROME_ORIGIN`.
8. **Chrome renders styled** (commits `993c44ebe3`, `5cc5674bea`, live-verified).
   The views wrote Tailwind classes as Mithril dotted selectors
   (`m("p.text-sm...")`), which Tailwind v4's scanner cannot parse, so every
   layout utility was purged and the chrome rendered as bare HTML. All views now
   use explicit `class:` attributes.
9. **Set-master-password copy** (commit `9901d24c47`, live-verified). Now
   explains the Honest Software model ("we cannot see your data ... pick a master
   password that we don't know") with a learn-more link **placeholder**
   (`HONEST_SOFTWARE_LEARN_MORE_URL = "#"`).
10. **One-time health-probe console note** (commit `2cafdbf026`, live-verified,
    fires exactly once). Explains the transient "CORS request did not succeed /
    status (null)" errors the `/_health` poll produces while a workspace is
    coming online (the browser logs the failed cross-origin fetch itself; page JS
    cannot suppress it), so they are not mistaken for bugs.
11. **`system_interface` terminal-origin fix** (dwt commit `34699e76`,
    unit-tested; live-confirmed passively). `labelForService` fell back to the
    bare service name whenever `apps.toml` had not loaded yet, so a terminal/
    service tab restored from a saved dockview layout before `apps_updated`
    arrived mounted an unroutable `terminal.<domain>` origin (a 403 on a share;
    invisible locally, where the forwarder routes bare names). The layout-restore
    path now awaits a bounded `whenAppsLoaded()` before re-deriving origins, and
    `labelForService` warns loudly instead of silently emitting a bare name.
12. **Two Modal acceptance tests marked `@pytest.mark.flaky`** (commit
    `5823fec302`) for the fresh-sandbox sshd boot race (paramiko "Error reading
    SSH protocol banner"), which outlasts mngr's deliberately-bounded banner
    retry. Not a branch regression — infra flake; the retry bound is left intact.

Autofix ran cleanly over every commit range this session (markers in
`.reviewer/outputs/autofix/*_verified.md`, latest `2cafdbf026`).

## Remaining work

### 1. Implemented but not yet live-verified end to end
- **Terminal fix (#11) in a real browser** — the mechanism is proven (labeled
  origin routes, bare-name doesn't; no bad request during create+enter) but the
  interactive open-terminal-then-reload restore-race was not force-reproduced
  through the cross-origin iframe. Worth one clean pass on the fresh slice.
- **Desktop create toggle (#6)** and **desktop imbue_cloud share-enable
  delegation (#7)** — unit-tested only; never driven through a running desktop
  Electron client.
- **Mint modal (`minds:open-ai-keys-page`) end to end** — still completely
  unverified. Needs an **ally-plan account** (the explorer test account has
  `monthly_llm_spend_usd=0`, so key minting is refused). Drive it from the
  in-workspace "Sign in with Imbue" surface.

### 2. Not implemented — the one real feature gap
- **Grants single-writer migration (spec phase 6).** Desktop grants reads/writes
  still go through `mngr exec` rewrites of `share_grants.toml`; they must move to
  owner-exec's `GET/PUT /grants` over the local forward channel so the workspace
  is the single writer. Needs: Python Ed25519 envelope signing mirroring dwt's
  `owner_exec/signing.py` (audience = share domain, +/-60s window, nonce cache);
  per-provider key discovery (remote rows expose the key path via
  `backend_resolver.get_ssh_info`; local docker/lima needs a plan); transport via
  the mngr-forward origin (the browser-side `ExecClient` shows the envelope
  contract); a fallback for pre-owner-exec workspaces; and the first-enable
  ordering flip (owner-exec refuses while the workspace is unshared, so
  grants-before-materials must reorder for that path). Design + implementation +
  tests, not a tweak.

### 3. Infrastructure / operational
- **Dev relay topology.** `dev1` now serves dev-josh-2 only, so dev-josh-1 (and
  any other dev env) shares are broken. Fix: per-env relays (region label = env
  name?) or a shared-dev frps auth scheme. Blocks any other dev env from sharing.
- **Shared dev box capacity.** Web-create needs a pre-baked available slice, and
  the box hovers near 6/6 across envs, so a consumed slice silently reblocks
  create with a 503 "no matching agents". Not a code bug; operationally fragile
  for dogfood. Bake more / dedicate capacity if this bites.
- **Staging / production rollout.** Add `[web_workspaces]` pins to those tiers'
  `deploy.toml`; provision a production chrome custom domain (`minds.imbue.com`
  as a second Modal custom domain — nothing in the repo configures custom domains
  yet); re-bake pools from a release tag. **Every existing session on every tier
  must re-login once** for the new partitioned accounts cookie — fold into the
  release notes.

### 4. Deferred spec items (tracked, not started)
- **Docs**: `apps/minds/docs/web-client.md`; security-boundaries notes (exec
  channel = `authorized_keys` possession; pool-key scope now covers adopt +
  share-materials injection + the `apps.toml` entry-label read; DEK-in-tab threat
  model; the `SameSite=None; Partitioned` accounts-cookie decision);
  overview/design sharing-section updates.
- **CI E2E** (Playwright vs a ci env) + `apps/minds/deployment_tests` extensions
  (web create end to end, owner entry, desktop adoption of a web-created
  workspace, destroy + retention). None of the web loop is covered in CI today.
- **Uncrystallized edge-case tests**: claim retry after partial adopt; tab death
  at each create step; wrong master password vs corrupt bundle; two tabs
  unlocking concurrently; desktop + web editing one record within a CAS window;
  workspace with share materials but a dead tunnel; enable-sharing rotate
  semantics on an already-shared host.
- **Safari / mobile partitioned-cookie matrix** — verified on Chromium only; the
  new-tab fallback exists but the minimum-Safari-version question (phase 1) is
  open.

### 5. Smaller follow-ups flagged this session
- **Chrome sourcemaps** — `build.sourcemap` is unset in
  `frontend_web/vite.config.ts` (repo-wide pattern), so our own bundle ships
  minified with no maps; enabling them would make the chrome debuggable.
  (Deferred at Josh's request; noted so it is not lost.)
- **Honest Software learn-more link** is a `"#"` placeholder pending the real URL.
- **`/_health` polling noise** — the one-time console note (#10) is the chosen
  mitigation; the deeper option (gate cross-origin probing behind a same-origin
  readiness signal from `GET /shares/{host}/status.last_tunnel_login_at`, so
  *fewer* failing requests fire) remains available if the noise still bothers.
- **Low-confidence review notes** in `.reviewer/outputs/autofix/issues/*.jsonl`
  (e.g. a resumed create regenerating `RESTIC_PASSWORD` — harmless before
  `restic init`, worth a look).

### 6. Merge / release
- Land the stack: merge PR #319, then PR #320, ultimately to `main` (currently 3
  branches deep off `main`).
- Merge the paired dwt branch `mngr/final-web-details` to dwt `main`, then fold
  it into the minds release (vendored-mngr sync + a release tag + pool re-bake)
  before any non-dev tier can use this.

## Gotchas for the next agent

- `minds env deploy` needs the `--deploy` activation mode; plain activate refuses.
- `just bake-slice-dev <region> "" 1 --server-id <id>` — the empty
  `workspace_dir` arg falls back to `.external_worktrees/default-workspace-template`
  and stamps THAT checkout's branch into the row attributes; keep it on the
  branch matching the deploy pin or the claim 503s.
- The box counts slice disks across ALL envs; `admin server list` shows only
  your env's rows, so "N/6 used" can understate the real occupancy — a bake can
  still fail with "6 in use on the box across all envs".
- The connector's per-env josh session lives under
  `~/.minds-dev-josh-2/mngr/profiles/*/providers/imbue_cloud/sessions/`; the CLI
  needs `--connector-url https://minds-dev-dev-josh-2--rsc-dev-api.modal.run`
  (activation does not export it).
- Browser-created workspaces are leased to the **browser** account
  (SuperTokens session), not the josh CLI account — so `mngr imbue_cloud hosts
  list` (josh) won't show them, and only that browser account can destroy them.
- A share row with no `entry_label` (pre-migration-023, or a workspace whose
  services had not registered) is not enterable from `/web`; re-enable sharing
  to record one.
- Changelog gate diffs against the PR base; run it locally with
  `GITHUB_BASE_REF=mngr/finish-web-only uv run python -m scripts.check_changelog_entries`.
- Regenerate SPA crypto vectors only via
  `uv run python apps/remote_service_connector/frontend_web/scripts/generate_crypto_vectors.py`.
- Verifying the web loop with Playwright: `/snap/bin/chromium --no-sandbox`; the
  accounts page defaults to the "Create account" tab, so scripted sign-in must
  click "Sign in" first; dev Turnstile auto-passes; a fresh browser context is
  needed to pick up the new partitioned cookie.
