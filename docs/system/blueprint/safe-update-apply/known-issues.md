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
mapping. The twelve issues below were open when this review was written and
have since been fixed on this branch (dwt PR #454 plus the paired mngr branch
for the CI job); each carries a "Fixed:" note saying how, and where the fix
diverges from the direction agreed here it says so. The bug-report collector
change under issue 4 is the one item deliberately left to a separate change.

## Issues (all fixed; original analysis kept for the record)

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

**Fixed:** `setup_system.sh` exports `HOME=/root` at the top, so every
installer that follows `$HOME` (claude, uv, `uv python`/`uv tool`) lands where
the script's checks and PATH entries look, on every invocation -- the
"single location" variant rather than a per-installer pin.

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

**Fixed:** the restic install decompresses to a `mktemp` file beside the
target and `mv -f`s over it, the same rename motion `install_downloaded_binary`
uses.

### 3. Provisioner bugs have no pre-apply validation

The structural version of 1 and 2: nothing in the flow exercises
`setup_system.sh` before the live apply. The worker cannot do it -- it shares
the live container, and there is no nested container to run the provisioner
in -- so the validation has to come from CI, and the apply has to stop
turning provisioner failures into whole-release walls. Three directions,
which compose:

- **A DWT CI "live re-provision" job.** CI today builds the image from
  scratch, which only exercises the fresh-build path; both real bugs are
  specifically *live re-run* bugs (ETXTBSY needs a running `host_backup`
  holding the binary; the `$HOME` split needs an agent-like env). Boot a
  container from the base image with services up, then run the PR's
  `setup_system.sh` inside it the way an agent would (`HOME=/home/user`,
  agent PATH). Catches this class at PR time.

- **Canonical env for the apply's provisioner call.** `apply` should invoke
  `setup_system.sh` with the env the image build used (`HOME=/root`, explicit
  PATH) rather than ambient agent env. Closes the divergence class generically
  rather than the one Claude symptom; do it alongside issue 1.

- **Provisioner failure alone should not roll back the merge.** A failed tool
  install leaves the tree and services consistent, and re-running the
  provisioner is cheap and merge-independent. On provisioner failure,
  continue to restart + probes; if healthy, land the update with a loud
  "provisioning incomplete: <step>" record (the `emergency.json` surface
  already exists for this shape) and have the skill re-run the provisioner
  after the fix; if the probes fail, roll back as now. Load-bearing
  provisioner changes (a node bump, a new apt dependency) still fail the
  probes and still roll back. This changes the exit-code contract and needs
  its own SKILL guidance.

**Fixed, all three directions.** The CI job lives in mngr-internal CI (where
the template image is actually built), not dwt CI: a new
`minds_snapshot_resume` test in `apps/minds/test_snapshot_resume.py` re-runs
the paired template's `setup_system.sh` inside the resumed workspace container
with services up, under `HOME=/home/user` and with the provision guard cleared,
while holding the pinned restic binary executing -- so both incident classes
fail the PR that introduces them. The apply invokes the provisioner with a
canonical env (`HOME=/root`, explicit PATH; `provisioner_env()`), on the
forward run and both recovery re-runs. And a provisioner failure alone no
longer rolls the merge back: the apply continues to the restart and probes; if
they pass, the update lands with a durable
`data/.state/update-apply/provision-incomplete.json` record (its own file
rather than `emergency.json`, whose banner means "may be broken") and a loud
stderr line, and SKILL 5b tells the lead to fix the cause and re-run the
provisioner; if they fail, it rolls back as before. The exit code stays 0 for
the landed-with-gap case; the stderr and the record are the contract.

### 4. Successful teardown destroys the worker the bug collector needs

SKILL §6 has the lead run `create_worker.py destroy --name update-self`
immediately after a successful apply. Incident B showed why that hurts: the
bug-report collector only captures `workspace/chats/`, so the worker's
transcript -- the primary evidence for half the issues in this document --
was only recoverable by manual archaeology in the destroyed worktree's
projects directory. Post-rollback the SKILL already keeps the worker; the
success path should stop being the destructive one.

Direction: do not destroy right away -- stop the worker so it stops consuming
memory but stays discoverable for the bug collector. Concretely:

- `create_worker.py` gains a `stop` subcommand (`mngr stop <name>`), and
  SKILL §6's success path uses it in place of `destroy`.

- `create_worker.py launch` gains `--destroy-existing`: `mngr create` refuses
  a duplicate name, so a kept-around stopped `update-self` worker would
  otherwise block the next pass. With the flag, launch destroys a previous
  *stopped* worker of the same name as a pre-flight step and still refuses a
  *running* one (that is a genuine conflict). The report-consumption guard is
  independent of the agent's existence, so keeping the agent does not by
  itself break the next pass's report handling.

- The bug-report collector (`system/scripts/collect_bug_report_diagnostics.py`)
  has to stop excluding workers. Today `list_chat_agents` drops every
  `agent_created=true` agent, so even a live worker is never attached. The
  cheap fix: include `agent_created=true` agents whose transcript was written
  inside the existing recency window (a few lines in `list_chat_agents` /
  `collect_transcript_members`; the recency filter already exists).
  `fetch_transcript` goes through `mngr event`, which reads `events.jsonl`
  under the host dir rather than talking to the agent process, so a stopped
  worker's transcript is reachable through the same path with no new code.

- Looking for *destroyed* agents' transcripts is not worth it: after
  `mngr destroy` the only surviving copy is the harness's own raw JSONL under
  the Claude projects directory (harness-specific path mangling, not the mngr
  common-transcript format), and finding it means scanning the filesystem
  and guessing at agent identity -- exactly the second discovery path the
  collector's design rejects in favour of asking mngr. Keeping the worker
  alive-but-stopped makes the mngr path sufficient.

**Fixed (except the collector):** SKILL §6's success path runs `mngr stop
update-self` directly (no new `stop` subcommand -- the mngr command is the
right tool), and `create_worker.py launch --destroy-existing` destroys a
previous STOPPED worker of the same name pre-flight while still refusing a
RUNNING/WAITING one; SKILL §3b passes it. The bug-report collector change is
being made separately.

### 5. `apply` runs `npm ci` live even when the worker's bundle will be installed

In `apply_update`, `if plan.frontend_manifest:` triggers `npm ci`
unconditionally before `_install_or_build_bundle` decides to just copy the
worker's already-built `static/`. That is the slowest, most memory-hungry
step on the critical path, it is tagged `as_expendable` (so a shed rolls the
whole update back), and the artifact is unused whenever `--worker-bundle` is
passed. Incident A's best-supported failure hypothesis is exactly a live
frontend build dying under load.

Fix: gate `npm ci` on the live-build fallback actually being needed.

**Fixed:** the apply decides up front whether the worker's bundle will be
installed (index present and its source stamp matches the merged tree -- see
issue 7) and skips `npm ci` when it will.

### 6. Flat 30-second pre-flight and health budgets

`_PREFLIGHT_ATTEMPTS = _HEALTH_ATTEMPTS = 30 x 1s`, carried over unchanged
from the old reveal. A loaded workspace boots a healthy backend slower than
that, and the observed outcome is "your change was bad" over a change that
was fine -- now with the whole release as blast radius and a retry that is
correctly refused (`_has_rollback_since`). The plan's own OOM-banding
reasoning says the apply runs when the box is under the most pressure.

Fix: there is no good progress signal to key off yet, so just raise the
budgets -- generously -- and tune them down against live testing and
benchmarking of real applies. A budget that is too long costs seconds on a
genuinely broken change; one that is too short rolls back a whole release.
The per-phase timings from issue 10 are the benchmarking input.

**Fixed:** both raised to 240 x 1s, with the marker's per-phase timings
(issue 10) as the input for tuning them down.

### 7. Nothing verifies the served bundle corresponds to the merged source

`_assert_bundle_built` only asserts `index.html` exists and `probe_frontend`
only asks whether the UI serves. A `--worker-bundle` pointing at a
stale-but-populated directory is copied silently and passes both -- the same
"source updated, UI didn't" state Incident A's user caught by eye after two
false success claims.

Fix: stamp the bundle at build time with the identity of the source it was
built from -- the git tree hash of the frontend source directory is the
natural one, since the worker builds from its branch tip and that tree must
equal the merged tree's for the same directory -- and have
`_assert_bundle_built` compare the stamp against the merged tree. A stale
`--worker-bundle` then fails the apply before restart instead of being
served.

**Fixed, with one divergence.** The frontend build stamps its output via an
npm `postbuild` step (`static/.source-tree-hash` = `git rev-parse HEAD:./`
from the frontend dir; best-effort, absent without a git repo), so worker
builds and live builds carry it without any guide change. The apply compares
it against `HEAD:system/apps/system_interface/frontend` of the merged tree.
Divergence: a stale or unstamped `--worker-bundle` **falls back to a live
build** (with a stderr note) rather than failing the apply -- the live build
produces the correct bundle, and failing would have turned a passable apply
into a whole-release rollback whose retry needs a fresh worker pass; it also
handles update-system-interface's ordinary merge, where a local frontend
change since the worker branched makes the worker's bundle legitimately
stale. A *live build* whose bundle does not match the merged tree does fail
the apply before restart, as agreed. When git cannot resolve the merged
frontend tree, verification degrades to the old index-only acceptance with a
warning.

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

**Fixed:** `"data"` added to the root `norecursedirs`.

### 9. `classify-merge` silently reports empty on a degenerate base

The worker guide's `classify-merge --local HEAD^1` is only correct while HEAD
*is* the merge commit. After the worker adds any commit on top, a re-run
collapses `merge-base(HEAD^1, target)` to the target and silently prints an
empty classification -- zero changed files over an 818-file merge in
Incident B. That worker caught the contradiction itself; a less careful one
reports an empty impact set.

Fix: a loud error in `_cmd_classify_merge` when `--local` already contains
`--target` ("did you mean the merge commit's first parent?").

**Fixed:** `_cmd_classify_merge` exits 1 with a plain `error:` line when
`--target` is already an ancestor of `--local`, pointing at the merge commit's
first parent.

### 10. The apply's duration is unbounded -- a hang looks like slowness

Incident A's reveal ran for 1h28m before the user asked "are you stuck?".
The problem is not that it was silent; it is that nothing about an update
should take anywhere near that long, and nothing stopped it. The reveal's
output never reached the transcript, so what hung is not determinable
(candidates: `npm ci`/`npm run build` shed or stalled under load, a stuck
pre-flight boot). The new apply is the same shape: one foreground command
with no per-phase deadline and no record of how long each phase took.

Fix: (a) record a timestamp per phase transition in the apply marker (it
already tracks `phase`), so every apply yields per-phase durations and the
next hang names its phase; (b) put a per-phase wall-clock budget on the
forward steps, sized from those measurements, so a hung step becomes a
rollback with a named phase instead of an open-ended wait; (c) gating the
live `npm ci` (issue 5) removes the largest known cost from the critical
path. Progress reporting to the chat is secondary: with (b), the command
returns.

**Fixed:** (a) the marker records `phase_timings` (phase -> epoch seconds at
each transition), and every apply prints an `apply phase timings:` line on
success and on rollback; (b) every forward step has a wall-clock budget
(`npm ci`/build/each env refresh 1200s, provisioner 1800s, restart 600s,
env-converge 1200s) whose expiry is an `ApplyFailed` naming the step -- the
provisioner's expiry is a recorded provisioning-incomplete failure per issue
3; recovery steps carry none; (c) issue 5's gating.

### 11. Pre-flight leaks the caller's agent env into the throwaway boot

`_preflight` passes full `os.environ`, including `MNGR_AGENT_ID`, to the
throwaway backend boot. The old reveal's preview path deliberately dropped it
so the preview could not clobber the live `layout.json`; the pre-flight never
had that guard and still does not. Pre-existing, low priority.

**Fixed:** `_preflight` drops `MNGR_AGENT_ID` from the boot env.

### 12. Worker-guide autofix scope is impractical for large merges (minor)

Guide 4c asks for full-scope autofix; over an 818-file merge Incident B's
worker sensibly scoped fix effort to the four reconciled files and flagged
its own divergence. Codify that scoping so a well-behaved worker is not
off-guide.

**Fixed:** the worker guide's 4c run branch is scoped to every file whose
merged content differs from the target release (hand-resolved conflicts,
in-branch edits, regenerated lockfiles); widening is allowed, narrowing below
that set is not, and the report names the scope.

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
