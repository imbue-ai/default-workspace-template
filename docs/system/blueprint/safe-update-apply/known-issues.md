# Known issues in the update-self flow (consolidated incident review)

Status as of 2026-08-25, reviewed against `gabriel/safe-update-apply` (dwt
PR #454). This consolidates the findings from two real update incidents plus
one process gap surfaced during their investigation:

- **Incident A** -- a minds workspace updating to minds-v0.4.1 under the *old*
  flow ("workspace2" transcript). The reveal silently failed, its
  `--rollback-to` reverted the entire 2,527-file update, a later retry
  reported "nothing to reveal" over the reverted tree, and the user was left
  with a broken chat interface for ~55 minutes while the agent claimed success
  twice.
- **Incident B** -- Sentry event `4cf0919b9dc74b8f98ef9bc049e9bb66`, an
  update-self pass on the geebspace production workspace. Sources: the bug
  report's updater transcript plus the update-self *worker* transcript, which
  the bug-report collector missed (it only captures `workspace/chats/`; the
  worker's transcript was recovered by hand from the destroyed worktree's
  Claude projects directory on the host).

The branch prevents most of Incident A outright and the redundant-work half of
Incident B; the "Fixed by this branch" section at the bottom records that
mapping. Everything above it is still open.

## Open issues

Ordered by how badly they interact with the new atomic-apply flow. A key
asymmetry to keep in mind for the first three: the worker validates the merge
in isolation but never runs the provisioner -- `setup_system.sh` only executes
at live apply time, so provisioner bugs are invisible until the apply, where
any failure now rolls back the whole merge (exit 2) and the same merge cannot
be re-applied without a fresh worker pass.

### 1. Claude pin bumps fail deterministically at apply time (provisioner `$HOME` divergence)

`setup_system.sh` installs the pinned Claude via claude.ai's installer, which
follows `$HOME` -- `/home/user` when run by an agent -- while the version
check and PATH use `/root/.local/bin/claude`. On a live re-provision the
installer "succeeds" into the wrong home and the check exits 1 (this is
exactly what Incident B's updater hit and hand-fixed mid-flight). Under the
new flow the apply runs `bash setup_system.sh` with ambient env inside the
atomic motion, so the next Claude pin bump fails the apply *every time*,
rolls back the entire release, and cannot re-land until the script is fixed
-- and the old escape hatch (the agent hand-fixing mid-flight) is precisely
what the atomic design removes.

Fix: pin the installer's target home (e.g. `HOME=/root bash
/tmp/install_claude.sh ...`) or derive install, check, and PATH from a single
location. Cheap, and it belongs in this release: the apply runs the *merged
tree's* provisioner, so shipping the fix alongside safe-update-apply means the
first new-flow update already runs the corrected script.

### 2. restic install races the live binary (ETXTBSY on re-provision)

`setup_system.sh` writes the restic binary with `bunzip2 -c /tmp/restic.bz2 >
/usr/local/bin/restic`, truncating it in place while `host_backup` may be
executing it -- ETXTBSY, observed in Incident B. The script already has the
right tool for this (`install_downloaded_binary` exists specifically for
ETXTBSY on live re-provisioning); restic bypasses it only because of the
bunzip2 decompression step. Under the new flow this transient race costs a
full rollback plus a fresh worker pass instead of a one-line retry.

Fix: `bunzip2 -c /tmp/restic.bz2 > /tmp/restic.new && mv -f /tmp/restic.new
/usr/local/bin/restic` (mv-over is what `install_downloaded_binary` does).
Same ship-with-this-release logic as issue 1.

### 3. Provisioner bugs have no pre-apply validation

The structural version of 1 and 2: nothing in the flow exercises
`setup_system.sh` before the live apply. Worth keeping in mind for any
provisioner-touching release; a worker-side smoke run (even syntax/lint plus
a dry-run of changed sections) would convert apply-time walls into
worker-time findings. No concrete design yet -- recorded so the asymmetry is
not rediscovered per incident.

### 4. Successful teardown destroys the worker the bug collector needs

SKILL §6 has the lead run `create_worker.py destroy --name update-self`
immediately after a successful apply. Incident B showed why that hurts: the
bug-report collector only captures `workspace/chats/`, so the worker's
transcript -- the primary evidence for half the issues in this document --
was only recoverable by manual archaeology in the destroyed worktree's
projects directory. Post-rollback the SKILL already keeps the worker; the
success path should stop being the destructive one.

Direction (per gabriel): do not destroy right away -- `mngr stop` the worker
so it stops consuming memory but stays discoverable for the bug collector.
Design considerations:

- `mngr create` refuses a duplicate name, so a kept-around stopped
  `update-self` worker blocks the next pass's launch. Either rename the
  worker before stopping it, use unique per-pass worker names, or have
  `launch` destroy a previous *stopped* worker of the same name as a
  pre-flight step.

- The report-consumption guard is independent of the agent's existence
  (launch refuses on a leftover file at `finish_report_path`), so keeping the
  agent does not by itself break the next pass's report handling.

- Complementary fix regardless: teach the bug-report collector to gather
  worker transcripts. They persist in the Claude projects directory even
  after `mngr destroy`, so the collector could capture them without any
  lifecycle change.

### 5. `apply` runs `npm ci` live even when the worker's bundle will be installed

In `apply_update`, `if plan.frontend_manifest:` triggers `npm ci`
unconditionally before `_install_or_build_bundle` decides to just copy the
worker's already-built `static/`. That is the slowest, most memory-hungry
step on the critical path, it is tagged `as_expendable` (so a shed rolls the
whole update back), and the artifact is unused whenever `--worker-bundle` is
passed. Incident A's best-supported failure hypothesis is exactly a live
frontend build dying under load.

Fix: gate `npm ci` on the live-build fallback actually being needed.

### 6. Flat 30-second pre-flight and health budgets

`_PREFLIGHT_ATTEMPTS = _HEALTH_ATTEMPTS = 30 x 1s`, carried over unchanged
from the old reveal. A loaded workspace boots a healthy backend slower than
that, and the observed outcome is "your change was bad" over a change that
was fine -- now with the whole release as blast radius and a retry that is
correctly refused (`_has_rollback_since`). The plan's own OOM-banding
reasoning says the apply runs when the box is under the most pressure.

Fix: scale the budgets or key them off progress (e.g. process-alive plus
port-open milestones) rather than a flat wall clock.

### 7. Nothing verifies the served bundle corresponds to the merged source

`_assert_bundle_built` only asserts `index.html` exists and `probe_frontend`
only asks whether the UI serves. A `--worker-bundle` pointing at a
stale-but-populated directory is copied silently and passes both -- the same
"source updated, UI didn't" state Incident A's user caught by eye after two
false success claims.

Fix: a build-stamp comparison in `_assert_bundle_built` (e.g. stamp the
merge SHA into the bundle at build time and assert it matches).

### 8. Staged worker copy breaks bare `uv run pytest` (test-file basename collision)

The staged skill-at-target copy under `data/.tasks/` ships
`update_self_test.py`, and the template root pytest config recurses into
`data/`, so collection dies on an import-file mismatch with the in-tree copy.
Incident B's worker diagnosed it and worked around with `--ignore=data`; it
recurs on every worker run, and this branch makes the staged test file much
larger.

Fix: add `"data"` to `norecursedirs` in the template root `pyproject.toml`
(it is runtime scratch, never part of the suite). A worker-guide note about
`--ignore=data` is the fallback.

### 9. `classify-merge` silently reports empty on a degenerate base

The worker guide's `classify-merge --local HEAD^1` is only correct while HEAD
*is* the merge commit. After the worker adds any commit on top, a re-run
collapses `merge-base(HEAD^1, target)` to the target and silently prints an
empty classification -- zero changed files over an 818-file merge in
Incident B. That worker caught the contradiction itself; a less careful one
reports an empty impact set.

Fix: a loud error in `_cmd_classify_merge` when `--local` already contains
`--target` ("did you mean the merge commit's first parent?").

### 10. The apply is one long silent foreground command

Incident A's reveal produced 1h28m of silence before the user asked "are you
stuck?". The new apply is still a single long-running command driven from the
chat with no progress channel. Mitigated substantially by the marker,
recovery cron, and rollback honesty, but the silence itself is unchanged.
Lower priority; a phase-progress line to the transcript (the marker already
tracks phase) would cover most of it.

### 11. Pre-flight leaks the caller's agent env into the throwaway boot

`_preflight` passes full `os.environ`, including `MNGR_AGENT_ID`, to the
throwaway backend boot. The old reveal's preview path deliberately dropped it
so the preview could not clobber the live `layout.json`; the pre-flight never
had that guard and still does not. Pre-existing, low priority.

### 12. Worker-guide autofix scope is impractical for large merges (minor)

Guide 4c asks for full-scope autofix; over an 818-file merge Incident B's
worker sensibly scoped fix effort to the four reconciled files and flagged
its own divergence. Codify that scoping so a well-behaved worker is not
off-guide.

## Fixed by this branch (for the record)

- Redundant duplicate apply work by the lead (Incident B issue 3): SKILL 5b/5c
  collapse into one classifier-driven `apply`; the manual per-class steps are
  gone.
- Ledger written before the update applied (A): `write_version_history_entry`
  runs inside `apply_update`, post-success only, idempotent.
- Dirty tree resolved by discarding it (A): SKILL 1 now surfaces and stops.
- Long merge-to-restart skew window (A, and the geebspace class generally):
  one apply owns merge through probe; `classify_path` marks
  `.mngr/settings.toml` and vendored-mngr source `requires_restart`.
- uv tool `$HOME` shadow-environment repairs (A): `_uv_tool_env` resolves
  `UV_TOOL_DIR` from the shebang of the binary actually on PATH;
  `_refresh_backend_dependencies` refreshes all three environments. (The same
  `$HOME` class remains unfixed in the provisioner -- open issue 1.)
- Plugins stripped by a bare `--reinstall` (A): `_tool_extras` reads extras
  from uv's receipt; `_warn_extras_lost` is loud when it cannot.
- The retry that lied over a reverted tree (A, the worst moment):
  `_has_rollback_since` makes re-running a landed-then-rolled-back merge fail
  as a precondition with an explicit re-dispatch message.
- Chat 500s from code/config skew (A): `agent_discovery._get_mngr_context`
  passes `strict=False`; skew degrades to a logged warning instead of taking
  down the channel needed to finish the update.
- Nothing surfacing the skew to the user (A): `update_staleness.py` header,
  meta tag, and the three banner variants.
- Interrupted/abandoned applies (both incidents' class): full-information
  marker, boot + cron `recover --if-stale`, `emergency.json` for the one
  outcome that cannot self-heal.

Deliberately retained: whole-merge revert on any apply failure. Incident A
shows the blast radius, but the half-applied alternative is worse; the new
flow keeps the scope while making it honest (exit-2 contract, rollback
commit carrying the failure headline, post-rollback SKILL guidance).

Not a flow defect: Incident A's "environmental, will pass after provisioning"
test-failure dismissal that was never re-checked is agent discipline, not
something this flow can structurally prevent.
