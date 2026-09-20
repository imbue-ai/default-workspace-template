# minds-v0.6.2 (2026-09-17): cut, built, and rehearsed on staging, unattended

Tag pair: mngr `253e087a58` (the `mngr/build-0-6-2` release branch, `main` at
`127aacef98` plus this branch's commits; reachable on `main` as the second
parent of merge `771d419277`, PR #1099), default-workspace-template
`073a56eb2d` (DWT PR #617's merge into `main`), both `minds-v0.6.2`,
annotated; vendor-match verified against DWT `origin/main` after the merge
(`system/vendor/mngr` equals `git archive` of the mngr SHA modulo `.minds/`).
ToDesktop build `260917ohslvgoyj` (pre-merge launch-to-msg run 35181525401 on
the frozen pair `253e087a58` / DWT `07bed5f1b`, a 19 minute real build; the
tag run is recorded below).

Cut from `main` the same day PR #1072 (the seeded welcome chat and the rest of
onboarding part 2) merged, one day after `minds-v0.6.1`, carrying the ~660
commits in between: the welcome-chat onboarding flow, the start-flow and modal
polish, the brand color change, the chat-agent refactor's later phases, the
public-mirror overlay work, and the box-registry documentation. Run by an
agent overnight with Josh away; owners were not notified (staging only).
**Nothing was deployed to production and no channel moved.**

## What rode along in the release branch

- Version 0.6.2, `FALLBACK_BRANCH = minds-v0.6.2`, and the 0.4.0 wire-compat
  snapshot's window extended to 2026-10-16: `wire_types.py` is byte-unchanged
  since `minds-v0.6.1`, so no new snapshot was needed.
- **A release-blocking bug on `main`, found by the pre-merge launch-to-msg
  Slack permission flow** (run 35177721472 on the first cut, `e98178c64e`): a
  permission request names its chat (`MINDS_CHAT_ID`), and a seeded welcome
  chat's id belongs to its seed member rather than to any agent (dwt's
  `seed_chat` / `create_chat` launch the first real agent with a fresh id and
  `chat_id` / `chat_seq` labels). The desktop's `mngr exec <chat id>` and
  `mngr message <chat id>` therefore never resolved: the resolution nudge
  retried forever, the in-chat card stayed pending (the e2e re-opened the old
  request and read "already been processed"), and `_resolve_host_id` returned
  None, so an approval from the welcome chat would have answered 503. Every
  green launch-to-msg run since the welcome chat landed had
  `SKIP_SLACK_FLOW: 1`, so nothing had exercised the flow. Fixed in
  `253e087a58`: the backend resolver aliases an id that names no agent to the
  newest member carrying that `chat_id` label (every lookup by agent id, plus
  a public `resolve_agent_id`), the nudge runs `message_chat.py` on that agent
  while addressing the chat by id, and the missing-script check no longer
  matches the `No such file or directory` an mngr provider warning carries
  (which had masked the lookup failure as "no script").
- `test_live_reprovision_succeeds_inside_the_running_workspace` (the PR gate
  `test-minds-snapshot`, red on `main` since DWT PR #612) asserted no
  `[provision-guard]` line at all; dwt's guard now prints an informational
  `ignoring inherited PYTHON_VERSION` line on a run that proceeds. The test
  asserts on the guard's skip line only.
- mngr release-suite fixes that rode along: the real-Claude subagent tests
  gitignore the plugin's artifacts; `test_ask_simple_query` declares the rsync
  its teardown now performs; `mngr schedule add --provider modal` bootstraps
  the Modal environment (the provider stopped auto-creating it on load).
- Debian trixie guest-image pins (`20260722-2547` on both sides) and the apt
  snapshot timestamps (`20260725T000000Z`) needed no bump, so step 0 did not
  apply. No tier configures `lima_image_base_url`, so 8b did not apply.

## Verification

- mngr PR #1099 CI on `253e087a58` (run 35181484360): every job green,
  including `test-offload`, `test-offload-acceptance`, `test-docker` and
  `test-minds-snapshot`.
- The minds release dispatch (`run_minds_release_tests=true`, run
  35181526941, `template_ref` = DWT `07bed5f1b`): `test-minds-release`
  (`minds_deployment` plus the plain release tests) and `test-minds-snapshot`
  (`minds_services`, snapshot-resume) green; `build-minds-ci-env`,
  `warm-minds-pool-cache` and `destroy-minds-ci-env` green. The earlier
  dispatch on `ec19602ca3` (run 35179454004, before the permission fix) had
  also passed in full.
- DWT PR #617 CI: green on `07bed5f1b` and again on the merge commit
  `054158984` (an `ours` merge of `main`, whose only new commit was the
  scheduled run's `sync_vendor` refresh of mngr `127aacef98`; the tree is
  byte-identical to the tested one).
- Pre-merge launch-to-msg run 35181525401 on the pair: `build` (19 min),
  `macos_launch`, `launch_to_msg` (`SKIP_SLACK_FLOW: 0`; deny, re-request,
  approve, and the canned Slack body in the reply all passed) and
  `notify_slack` green.
- Tag run 35186747132 on `commit_sha=minds-v0.6.2` / `template_ref=minds-v0.6.2`:
  `build` reused `260917ohslvgoyj` for the identical mngr SHA, and
  `launch_to_msg` was a real 17 minute round-trip through the binary's baked
  `FALLBACK_BRANCH` with the Slack flow on. Green here concludes the artifacts.
- The mngr-wide `release-tests.yml` suite (run 35177741197 on the first cut)
  was red for reasons unrelated to minds and predating this branch (last
  green 2026-09-04); see "Not done" below.
- Two runs were cancelled to free the mac runner for the release: my own
  launch-to-msg on the superseded `ec19602ca3` pair, and the 04:12Z scheduled
  main-vs-main run 35181068292 (its `sync_vendor` job had already pushed the
  dwt `main` vendor refresh that made DWT PR #617 conflict). The cron runs
  twice daily.

## Staging tier (2026-09-17)

Everything below ran from the `mngr/build-0-6-2` checkout at `253e087a58`
(the tag), with Josh away.

- **Pre-deploy checks**: the 043 duplicate-address query against the staging
  pool DB returned no rows; schema was at 042; no leftover recover-target
  file; `minds-admin env activate --deploy staging` pinned `minds-staging`.
- **Services deploy**: deploy_id `20260917T054239Z`, RECREATE (migration
  `043_wireguard_address_unique.sql` applied), 05:42Z-05:5xZ, both health
  checks green, Neon snapshot deleted, recover-target removed. Ships the 0.6.2
  bump (the staging web-create pin is now `minds-v0.6.2`) and the welcome-chat
  permission fix. `/version` advanced from `20260915T021647Z`.
- **Fleet audit** before the bake: 3 boxes, 3 exclusive, 0 contaminated, keys
  0/0, CA correct, storage encrypted, no degraded arrays; free slots 2 / 13 /
  12.
- **0.6.2 bakes, one row per gen-2 box, concurrent, each a first-of-tag seed
  (06:0xZ-06:3xZ)**:
  - `21ae4720` (hil, US-WEST-OR): row `32838f51`,
    `host-7b37254ea6ed4e0288a5998003c45cb5`, ports 22002/22003, 6 vCPU.
  - `c7793839` (hil, US-WEST-OR): row `f28fc284`,
    `host-5aaa635fd9bb4a6ca64ac4283fd8e7ae`, ports 22000/22001.
  - `72dd8187` (vin, US-EAST-VA): row `8b090ce0`,
    `host-b48f156385bf44f2a00ce4ff7e6667d4`, ports 22004/22005.
  All 3/3 succeeded (8 units / 44 GB, runsc). Verified over the operator
  certificate with the runbook script: `DEFERRED_INSTALL_OK`,
  `system-services` STOPPED, git identity `minds-bootstrap`, `CONTENT_OK`,
  gVisor kernel, vendored version 0.6.2 on all three.
- **Pool afterwards**: 3 `available` 0.6.2 rows (2 US-WEST-OR, 1 US-EAST-VA);
  the 8 leased rows (0.4.1 to 0.6.1) untouched. Nothing retired.
- **Desktop**: `just minds-start-cloud` launched against staging from the
  release checkout (create form prefilled with `minds-v0.6.2`) for Josh to
  claim a row in the morning. **Not yet leased from a desktop**: the
  fast-path check (`adopted pre-baked agent` in `~/.minds-staging/logs/`)
  and the in-workspace checklist in app-release.md remain to be done by hand.

## Not done, deliberately

- The mngr-wide `release-tests.yml` suite is red for reasons predating this
  branch: the macOS shards lack GNU `timeout`, resolve `/tmp` to
  `/private/tmp`, and tmux reports `terminal does not support clear` on
  `connect`; codex's app-server readiness times out at 10 s on both runners;
  `agy` is not installed for the antigravity schema test; a transient
  nodejs.org 404 broke one Modal image build; `test_claude_agent_full_lifecycle`
  did not capture its seed turn. Three of its failure classes were fixed here
  (above); the rest are left for their owners.
- Production: nothing. The production gen-2 work in `next_deploy.md` is
  unchanged.
- The welcome-chat permission fix is a minds-side alias; whether dwt should
  instead launch a seeded chat's first agent under the chat's own id is a
  design question for the chat app's owners.
