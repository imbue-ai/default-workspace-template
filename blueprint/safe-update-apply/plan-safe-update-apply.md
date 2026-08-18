# Plan: safe-update-apply — invert update-self / update-system-interface and make the apply atomic

## Overview

- Invert the ownership between the two update skills: `update-self` gains a general "apply a merge safely" script (new `apply` / `recover` subcommands on its existing `update_self.py`), and `update-system-interface` calls that script for its apply step, keeping only the `preview` / `unpreview` adapters in `reveal_system_interface.py`.
- Motivation: two production incidents showed the current split is unsafe. Landing the merge (5b) is live-affecting on its own — the running system interface re-reads `.mngr/settings.toml` through old in-memory code, and the editable `mngr` tool points into the vendored tree the merge just replaced — while everything that repairs that lives in later prose steps across multiple agent turns. A pause between land and reveal (a user question, a stop hook, a crash) strands the workspace half-applied, and the failure kills the very chat channel needed to continue.
- The apply becomes one atomic, idempotent, rollback-on-failure motion inside a single OOM-protected process: merge, dependency refresh, provisioner run, build/install, restarts, health probes. On any failure it reverts the entire merge and restores the pre-apply dependency snapshots — a recovery path that needs no network, no package manager, and no working `mngr`.
- Interruption becomes detectable and self-healing: a full-info marker (DRI agent, rollback point, phase, PID) plus a flow-level "updating workspace" lease; bootstrap recovers at container start, a permanent stale-guarded cron entry catches kills without a restart, and the recovered workspace re-engages the DRI agent to communicate and offer a cheap retry (worker branch and report are preserved).
- Because the script lives inside `.agents/skills/update-self/`, the existing Step 2a bootstrap handoff stages it pre-merge — so old workspaces updating in run the *target's* apply, and fixes to the apply flow take effect for the very update that ships them.

## Expected behavior

### update-self happy path

- After the user approves (5a), the lead runs one `apply` invocation from the staged skill-at-target copy. It ff-merges the worker's `update-self:` merge commit, refreshes every affected environment, re-runs `setup_system.sh` when provisioner-classified paths changed (before any restart), installs the worker's already-built frontend bundle when available (the artifact the user previewed; live build as fallback), pre-flights, restarts, probes to the frontend standard, writes the VERSION_HISTORY.md ledger entry, and runs `env-converge upgrade` post-success. No agent-prose pause exists between the merge landing and the workspace being consistent with it.
- Exit-code contract stays `0` revealed / `2` rolled back / `3` emergency / `1` precondition, with the honest-closing-line variants from PR 409.

### Failure and rollback

- On any failure the script reverts the entire merge as a forward revert commit and restores the pre-apply snapshots: built bundle, root `.venv`, both uv tool environments, `node_modules`. All restores are file copies to their original absolute paths — no network, no `npm`/`uv`, no `mngr` required, so a broken build environment or a broken `mngr` cannot take the workspace down with it.
- Globally pinned tools (the `setup_system.sh` tier) roll back by re-running the provisioner from the restored tree, only when the apply had run it; if that re-run fails (e.g. no network), the rollback still counts as recovered and the closing report names the tools left ahead of the tree. `env-converge` runs post-success only, so a failed apply never moved apt state.
- The retry path survives every rollback: worker branch, worktree, and report are kept, so a diagnosed retry is a seconds-long re-land, not a re-run of the worker. The DRI agent's message offers exactly that.

### Interruption (hard kill) and recovery

- The script writes a marker under `data/.state/` at apply start — DRI agent (from its environment), rollback point, last completed phase, PID — and clears it on every exit path. A concurrent `apply` refuses to start while a live marker exists.
- Container restart: bootstrap checks the marker, runs the dependency-free rollback directly (no agent or UI needed), then wakes and messages the DRI agent to verify state and talk to the user.
- Killed without a restart and the DRI agent gone too: a permanent cron entry (installed at provision time, every ~5 minutes) runs `recover` with an only-if-stale guard — marker present, recorded PID dead, older than a grace period — and is a silent no-op in every normal state. It invokes the stdlib-only script directly, not the automations/agent machinery.
- The DRI agent, when alive, simply re-runs the idempotent `apply`; every step tolerates re-entry.

### Memory pressure

- The apply orchestrator is close to OOM-exempt: it bands itself well above every agent, chat, and ordinary service — losing the build is an ordinary failure the rollback absorbs, but losing the apply mid-motion is the half-applied state this whole design exists to prevent. Only the authority paths that would repair a failed apply (owner-exec, the terminal) stay below it.
- Subprocesses inherit that protection by default, which is what the recovery-critical steps need: git operations, snapshot copies and restores, service restarts, the provisioner, `mngr` invocations. During the forward apply, only the genuinely memory-hungry and cleanly recoverable steps — `npm ci` / `npm run build`, the uv installs, the pre-flight boot — are tagged back to the expendable band.
- During rollback and `recover`, nothing is tagged expendable: there is no further rollback to absorb a shed, so every recovery step keeps the orchestrator's protection (extending PR 409's banding split, via the same shared `oom_priority.bands` mechanism).

### Skew hardening (independent of the atomic apply)

- The system interface reads mngr config with `strict=False` at its single `load_config` call site, so a settings file written for a newer mngr degrades to a logged warning instead of a 500 on every send — the lockout that made the geebspace incident self-locking. `mngr config set` and all CLI paths keep strict parsing.
- The change classifier treats vendored-mngr *source* changes and `.mngr/settings.toml` changes as restart-requiring, so nothing live keeps reading config newer than its own code after an apply completes.
- The system interface records the tree HEAD it started from and stamps a response header when the live tree has moved under it; an informational banner tells the user "parts of this workspace were updated but not yet activated" or "an update was interrupted" (from the marker). Acting on it stays with the agent.

### Concurrency

- A general "updating workspace" lease (tk chore, like the other flows' leases) is taken by the lead at flow start and held through worker, approval, and apply — replacing the update-self single-flight ticket check. When the update touches the system interface, the existing `editing service system_interface` lease is additionally taken; that lease is unchanged for non-update SI edits.

### Migration-required updates

- When an update cannot be applied in place (agent judgment, standardized by prose — no mechanical epoch marker), the user-facing message stays high-level: "this update contains fundamental changes that can't be directly applied," followed by exactly two steps — create a new workspace, then message its agent `/migrate-workspace` with this workspace's name. No path tables, no hand-rolled migration plans. The refusal also offers the newest in-place-compatible release when one exists ("I can apply X now; Y needs the fresh-workspace migration").

### Cross-version behavior

- An old workspace updating in follows its local prose only through Steps 1–2; the Step 2a handoff stages the target's update-self skill (now containing the apply script) and everything from Step 3 on — worker dispatch, approval, apply — runs the target's flow. The apply script therefore tolerates executing against older pre-merge trees (guarded imports, no assumptions about pre-merge layout). No compatibility shim for the old `reveal --rollback-to` entry point is needed: its only call sites are in old prose that the handoff supersedes.
- The update-system-interface local-edit flow always uses its own in-tree copies, so its prose and the apply script stay consistent by construction.

## Changes

- `.agents/skills/update-self/scripts/update_self.py`: add `apply` and `recover` subcommands. `apply` owns merge (ff-only or ordinary), snapshotting, dependency refresh, provisioner run, bundle install/build, pre-flight, restarts, frontend/health probes, full-merge rollback with snapshot restore, marker lifecycle, ledger entry, and `env-converge upgrade`; `recover` is the boot/cron entry point with the only-if-stale guard. The system-interface reveal machinery (bundle snapshot, frontend probe, classification-driven actions) migrates here from `reveal_system_interface.py` so the staged copy is self-contained; the module stays runnable under bare `python3` against old trees.
- `.agents/skills/update-system-interface/scripts/reveal_system_interface.py`: shrinks to the `preview` / `unpreview` adapters.
- `.agents/skills/update-self/SKILL.md`: 5b + 5c collapse into the single `apply` invocation; the "updating workspace" lease replaces the single-flight ticket check; the SI editing lease is taken when relevant; migration-required canonical copy and the two-step `/migrate-workspace` path; retry guidance after rollback; composition rules for the DRI agent's recovery and rollback messages.
- `.agents/skills/update-system-interface/SKILL.md`: Steps 4–5 replaced by a call to the general apply (worker branch as the merge source); preview flow unchanged.
- `system/apps/system_interface`: `strict=False` at the `agent_discovery` config read; startup-HEAD staleness header and informational banner (reading the marker for the interrupted-update variant).
- Change classification: vendored-mngr source and `.mngr/settings.toml` become restart-requiring classes.
- OOM banding (`oom_priority.bands`): a near-exempt band for the apply orchestrator (above agents, chats, and ordinary services; below only owner-exec and the terminal); expendable tagging applied to the hungry forward-apply steps only, never during rollback/recover.
- `system/libs/bootstrap` (or its startup sequence): marker check + direct recovery + DRI agent wake at container start.
- Provisioning (`setup_system.sh` / `build_workspace.sh`): install the permanent recovery cron entry. (Verify crond runs in these containers.)
- Tests for the apply's control flow (recording-runner style, as PR 409's), the recovery paths, the marker/lease lifecycle, the classifier additions, and the SI strict/staleness changes; changelog entries per touched project.
- Lands as its own PR stacked on PR 409's branch (`gabriel/rigorous-slug`); 409 merges first, unchanged.
