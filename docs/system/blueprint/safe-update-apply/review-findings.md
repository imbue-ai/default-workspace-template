# safe-update-apply: pre-merge review findings (2026-08-25)

Findings from the full-branch review run before merge (both repos: this one
and the paired mngr-internal branch), curated down to the items judged worth
acting on -- pure test-quality and style nits were dropped. Everything here
was verified against the code at `gabriel/safe-update-apply`; none of it is
fixed on the branch yet. Line numbers are as of that review.

## Correctness — apply/recover (`.agents/skills/update-self/scripts/update_self.py`)

1. **A non-object `marker.json` crashes instead of degrading.**
   `ApplyMarker.from_json` calls `raw.get(...)` straight off `json.loads`, so a
   marker that parses to a list/string/number/null raises `AttributeError`,
   which is not in `read_marker`'s except tuple (`:1639`) — contradicting its
   own "a corrupt marker must not wedge that decision forever" docstring, and
   wedging `apply`, `recover`, and the recovery cron permanently.
   `bootstrap/manager.py:834` guards the same read with `isinstance(raw, dict)`;
   the script should too (or `read_marker` should catch `AttributeError`).

2. **`_run_provisioner`'s "never raises" holds only for `TimeoutExpired`.**
   A missing `bash` (or any `OSError` from `runner.run`) escapes at `:1784-1793`,
   and the forward apply's step block catches only `ApplyFailed` (`:3173`), so
   the exception skips the rollback and leaves the marker plus a half-applied
   tree — the state the design exists to prevent.

3. **The rollback handler itself catches only `CalledProcessError`**
   (`:3318-3341`): an `OSError` during `_restore_tree`/`_commit_rollback`
   escapes with no emergency record and the marker left behind.

4. **`recover` has no clean-tree/untracked handling.** `_commit_rollback`
   decides via `git status --porcelain` (which lists untracked files) but
   commits without `-a`; with nothing staged and any untracked file present,
   git exits non-zero → `recover` returns 1 and deliberately keeps the marker
   (`:3509-3522`). On the boot path that means recovery fails every boot, the
   DRI agent is never woken, and the "update interrupted" banner persists.

5. **The post-rollback env rebuild keys on `"venv"` alone** (`:2881-2882`),
   but `snapshot_targets` records `venv` and the two `tool-*` envs separately
   (`:1867-1875`), and an unresolvable tool env is silently skipped at snapshot
   time (`:1836-1849`, no "nothing to copy aside" note). A restored `venv` with
   a missing/failed tool-env restore leaves both uv tools built from the
   rolled-back-away tree while recovery reports success — exactly the
   `ModuleNotFoundError`-on-`mngr` state the workarounds doc describes.

6. **An apply that lands over a previously broken UI and confirms health never
   clears `emergency.json`** — the clear is gated on `is_frontend_expected`
   (`:3303-3309`), which is False precisely when the previous emergency left
   the UI down. The same run prints "confirmed healthy" while the
   `update-emergency` banner stays up until some later apply that starts
   healthy.

7. **The recovery-side provisioner re-runs bypass `_run_provisioner` and its
   1800s budget** (`_recover_running_state:2852-2859`, `recover:3527-3534`).
   A hung (not failing) `setup_system.sh` wedges recovery indefinitely; on the
   cron path it holds the `flock` forever, silently disabling the five-minute
   guard. Three divergent copies of the same subprocess call in one file.

8. **The ledger commit runs without `--no-verify`** (`:2641-2654`), unlike
   `_commit_rollback` (`:2480`) and bootstrap's machine commit. A failing
   pre-commit hook leaves `docs/VERSION_HISTORY.md` staged after the marker was
   already cleared, so every subsequent `apply`/`recover` fails its clean-tree
   precondition until someone cleans up by hand; the "record it manually"
   warning doesn't name the staged file.

9. **`env-converge upgrade` catches only `TimeoutExpired`** (`:3398-3413`); an
   `OSError` (e.g. `uv` unresolvable) turns a landed, healthy update into a
   traceback and a non-zero exit. The adjacent ledger block catches
   `(CalledProcessError, OSError)`.

10. **A `.mngr/*`-only change triggers a full `setup_system.sh` run**: every
    `.mngr/` path classifies as provisioner (`:628-631`), but the script does
    not read `.mngr/` at all — a settings-only release pays an 1800s-budget
    provisioner run and can leave a spurious `provision-incomplete.json`.

## Correctness — restart/staleness coupling

11. **Restart rule and staleness rule disagree on the two editable libs.**
    `update_staleness.py:93-106` counts `system/services/oom_priority/**.py`
    and `system/libs/tk_command_parsing/**.py` as making this server stale, but
    `classify_path` marks them `shared_runtime` with `requires_restart=False`
    (`update_self.py:651`), and `plan_apply`'s backend match misses them too.
    An update touching either (this branch itself changes `bands.py`) restarts
    nothing, so the `updated-not-activated` banner latches until something else
    bounces the services agent. The guard test
    (`update_staleness_test.py:449-472`) only checks the other direction, and
    its path list is hand-written literals, so drift fails nothing.

12. **Boot-path partial restore leaves no durable signal**: `recover
    --no-restart` returns 0 and clears the marker even when snapshot restores
    failed (`:3535-3555`); no `emergency.json` is written on that path, and
    bootstrap logs it at the same level as a clean rollback
    (`manager.py:947-967`). A workspace can boot with a rolled-back tree over a
    non-restored venv and surface nothing.

13. **The provision guard likely defeats the documented rollback re-provision**
    (`manager.py:908-912`): after `_commit_rollback` the tree hash equals the
    originally provisioned tree, so `provision_skip_if_done` exits 0 without
    reinstalling, leaving global tools at post-apply versions while the code
    rolls back — reported as a clean rollback.

## Robustness — staleness detector / server

14. `update_staleness.py`'s git "bound" is 10s + a 30s default shutdown grace
    per call, two calls per check (~80s worst case), it runs synchronously on
    every unmatched GET via the `/<path:path>` catch-all (not just page loads),
    with no memoization; and `git diff --name-only` output is not `-z`/quotePath
    protected, so non-ASCII paths are C-quoted and silently miss every prefix
    rule. The two editable-lib manifests (`oom_priority`, `tk_command_parsing`
    pyprojects) also fall outside `_BACKEND_MANIFESTS`.

15. `app_context.static_directory` has no production injector — only tests set
    it. Either wire it or drop it from the production model.

16. The staleness meta-tag injection interpolates without HTML escaping
    (`server.py:565`). Safe today (the only producer returns one of three
    module constants), but the parameter is typed `str | None`, so a future
    variant sourced from disk (e.g. text out of `emergency.json`) would be a
    stored XSS in the app shell. The adjacent base-path tag has the same
    latent shape.

17. The new `UPDATE_APPLY` band may not hold on the cron path: banding to 15
    *lowers* `oom_score_adj`, which needs `CAP_SYS_RESOURCE` when the process
    inherited a higher adj — and the cron parent is banded 55, so the write
    can fail with only a warning (`update_self.py:1204`). Separately,
    `oom_tag_backstop`'s raise-only descendant walk can lift a running
    `recover` from 15 back to its parent's 55 if cron's program is re-tagged
    mid-run.

## Tooling / tests

18. `system/services/oom_priority/bin/script_import_paths_test.py` no longer
    guards the script that actually bands itself: the reveal entry is vacuous
    (its `sys.path.insert` is gone) and `update_self.py`'s insert shape doesn't
    match `_PATH_INSERT_RE`, so the update-apply orchestrator is uncovered.

19. `create_worker.py --destroy-existing` refuses a `DONE` predecessor (an
    agent whose process exited on its own) while the docs claim only
    RUNNING/WAITING are refused (`:44-45` vs `:657`); and the documented
    "unreadable listing degrades to mngr create's duplicate-name refusal" is
    actually an uncaught `CalledProcessError` traceback (`launch` runs
    `mngr create` with `check=True` and `main` catches nothing).

20. `update-system-interface/SKILL.md:317-342`: the teardown/lease-release
    block is gated on success-or-rejection, so apply exits 1/2/3 leave no
    instruction to unpreview or release the lease — a rolled-back apply
    strands both.

21. Notable test gaps: no test pins `main()`'s
    recover-before-venv-sync/wake-after ordering in bootstrap; the
    cron-vs-boot flag split is only half-pinned; two "bundle restored"
    assertions are vacuous because the emulated build already recreated the
    bundle (`update_self_test.py:2072`, `:2554`); the recovery-refreshes-envs
    assertion passes on forward-pass `uv tool dir` calls alone (`:3157`);
    real-git ledger tests inherit the developer's global git config (gpgsign /
    hooksPath would break them); the starter-drift test permanently self-skips
    once any real ledger entry exists (`:3656-3676`); `_worker_is_idle`
    constructs its own `Runner()` instead of taking the injected one, so a
    launch-sync timeout "unit" test really shells out to `mngr list`; and the
    new SI frontend vitest files (like all frontend tests) never run in GitHub
    CI, so the banner's prototype-chain guard is unenforced there.

## Paired mngr-internal branch

22. `offload-modal-minds-snapshot.toml` comment says the lane runs 19 tests;
    the config's own selection collects 15 (the 4 `release`-marked tests are
    deselected), so the `max_parallel` 20→24 bump was unnecessary and the dev
    changelog repeats the wrong count. The new live-reprovision test would
    silently pass (provision guard skips, exit 0) if the hard-coded marker dir
    ever drifts from `_provision_guard.sh`; nothing asserts the script actually
    ran. Its 780s+120s budgets plus fixture time sit exactly at the 900s item
    timeout. The `apps/minds` changelog entry mis-describes the test's
    `HOME=/home/user` as "the way the update-self apply does" — the apply
    deliberately pins `HOME=/root`; the test models a hand-run.

## Architecture review of the combined branch (2026-08-25, `gabriel/tactful-swift`, both repos)

Findings from the architecture pass over the merged branch (this repo plus the
paired mngr-internal branch). None of these are fixed; they are decisions for
the author, not autofix material.

23. **The app's update-event consumer has no producer.** mngr-internal ships
    `UpdateEventConsumer`, the `update` source in `mngr_forward`'s
    `stream_manager.py`, the `forward_cli.py` routing, `parse_update_event`,
    and the `ALREADY_CURRENT` verdict, all tested -- but nothing on this branch
    appends to `$MNGR_AGENT_STATE_DIR/events/update/events.jsonl`. As paired,
    the apply window opens only via the stuck-edge race path, verdicts never
    arrive, and every run ends through the 20s liveness poll as STALLED, which
    the app files as a real failure (schedules cancelled, error-level log).
    "Update failed" is therefore the expected badge after a successful update.
    Cheapest coherent fix is on this side: `update_self.py apply` already has
    `_advance(phase)` and `write_marker`; appending an envelope line at start,
    before `mngr start --restart`, and at each return is small. The
    alternative is to hold the consumer, `ALREADY_CURRENT`, and the consent
    tiers out of the app PR until the emitter exists.

24. **The consent tiers bind to nothing.** The app's unattended seed prompt
    tells the agent to "stop at the gate and wait", but `update-self/SKILL.md`
    on this branch is "fully unattended" with no approval gate (the only
    interactive stop left is the over-ceiling `--override` confirmation, which
    unattended runs never send). So the 428 consent, `UpdateConsentKind`, and
    the `BACKUPS_NOT_CONFIGURED` skip change nothing in the workspace; and the
    attended "Update now" path also lands unattended, which the app's copy does
    not say. Decide whether the gate exists and make the app's copy match.

25. **Apply window vs. the apply's own budgets.** The app's
    `DEFAULT_APPLY_WINDOW_SECONDS = 360` (already marked `CLEANUP:` as a guess)
    is shorter than this side's `_RESTART_TIMEOUT_SECONDS = 600`, the
    240 x 1s health and pre-flight probes, and `DEFAULT_RECOVER_GRACE_SECONDS
    = 600`. A legitimately slow restart+health phase is handed to
    `dispatch_restart` mid-apply -- the misdiagnosis the window exists to
    prevent -- and the cron `recover --if-stale` then sees a dead pid. The
    probe already reads `marker.json`; driving the window from the marker's
    `phase`/`updated_at` against `DEFAULT_RECOVER_GRACE_SECONDS` puts both
    repos on one authority and one staleness rule.

26. **Inverted dependency on this side.** `system/libs/bootstrap` and the
    `/etc/cron.d/update-apply-recover` entry now depend on a script under
    `.agents/skills/` that re-points itself at other versions at runtime, and
    `update_staleness.py` re-implements a narrower `classify_path` because it
    cannot import from there. The apply/recover/emergency/provision-incomplete
    core (now ~3,900 lines in one stdlib-only file) would sit more naturally as
    a `system/libs/update_apply` package that the skill, bootstrap, cron, and
    the SI all import, with the marker path exported from one place (it is a
    literal in four files across the two repos today).

27. Smaller items on the mngr-internal side: `_build_workspace_update_service`
    constructs a second `UnattendedRecoveryDispatcher` for `dispatch_restart`
    instead of a flag on the registered one; `WorkspaceUpdateService.scheduler`
    is the graph's one mutable back-reference; `_is_update_request_authenticated`
    is the third copy of `is_ui_request_authenticated`; the detector uses a
    `ThreadPoolExecutor` where every other strand uses `ConcurrencyGroup`; and
    the version read now costs up to two execs where there was one. The
    `update-self: merge upstream template (<ref>)` commit subject is a prose
    contract between the worker doc and the app's version grep.
