# Plan: safe-update-apply — invert update-self / update-system-interface and make the apply atomic

## Overview

- Invert the ownership between the two update skills: `update-self` gains a general "apply a merge safely" capability (new `apply` / `recover` subcommands on its existing `update_self.py`), and `update-system-interface` calls it for its apply step, keeping only the `preview` / `unpreview` adapters in `reveal_system_interface.py`.
- Motivation: two production incidents showed the current split is unsafe. Landing the merge (Step 5b) is live-affecting on its own — the running system interface re-reads `.mngr/settings.toml` through old in-memory code, and the editable `mngr` tool points into the vendored tree the merge just replaced — while everything that repairs that lives in later prose steps across multiple agent turns. A pause between land and reveal (a user question, a stop hook, a crash) strands the workspace half-applied, and the failure kills the very chat channel needed to continue.
- The apply becomes one atomic, idempotent, rollback-on-failure motion inside a single near-OOM-exempt process: merge, dependency snapshots and refresh, provisioner run, build/install, restarts, health probes. On any failure it reverts the entire merge and restores pre-apply snapshots — a recovery path needing no network, no package manager, and no working `mngr`. (One deliberate exception: a failed provisioner run alone does not roll back — the apply carries on to the restart and probes and, if they pass, lands with a durable `provision-incomplete.json` record; the incident that motivated it is recorded in the mngr-internal tracker below.)
- Interruption becomes detectable and self-healing: a full-info marker (DRI agent, rollback point, phase, PID), a flow-level "updating workspace" lease, boot-time recovery in bootstrap, and a permanent stale-guarded cron entry for kills without a restart. The recovered workspace re-engages the DRI agent; the retry path (worker branch, report) survives every rollback.
- Because the script lives inside `.agents/skills/update-self/`, the existing Step 2a bootstrap handoff stages it pre-merge — old workspaces updating in run the *target's* apply, so fixes to the apply flow take effect for the very update that ships them.

## Expected behavior

### update-self happy path

- After the worker reports `done` and the lead's report audit passes (5a), the lead runs one `apply` invocation from the staged skill-at-target copy -- no user approval gate, and no agent-prose pause between the merge landing and the workspace being consistent with it.
- The apply: ff-merges the worker's `update-self:` merge commit, snapshots current state, refreshes affected environments, re-runs `setup_system.sh` when provisioner-classified paths changed (before any restart), installs the worker's already-built frontend bundle when available (the exact artifact the worker validated; live build as fallback), pre-flights, restarts, probes to the frontend standard, writes the VERSION_HISTORY.md ledger entry, and runs `env-converge upgrade` post-success.
- Exit-code contract is `0` applied / `2` rolled back / `3` emergency / `1` precondition, each with an honest closing line (a workspace whose frontend was already broken beforehand still lands and still exits `0`, but the closing line names the breakage instead of confirming health; a landed apply whose provisioner run failed is the other `0` variant — its contract is the stderr closing line plus the `provision-incomplete.json` record).

### Unattended operation

- The pass is fully unattended after launch: the user starts an update and can walk away; the only mid-flight stop is the customization-survival hold below. A user who initiates an update also wants it applied -- our average user holds no opinions on technical choices -- and everything the apply lands is git (usually with a host backup behind it), so post-hoc rollback replaces pre-approval.
- The old 5a approval gate becomes a **results message** composed after the apply: the same composition rules, now reporting what landed, every decision made on the user's behalf (conflict resolutions, provisioning caveats), and a standing rollback offer -- honored by landing a forward revert through the same apply machinery (`apply --merge-ref <revert branch>`, no `--target-ref`), with the Step 1 backup as the full-rewind fallback.
- A missing or unconfirmed pre-pass backup (`host-backup-now` exits 1/2/3) is flagged in the results message instead of blocking the pass: git plus the apply's own rollback and `recover` remain the recovery path.
- Merge-conflict questions the worker cannot settle are decided by the lead -- defaulting to preserving the workspace's local behavior -- recorded, and surfaced afterwards with the alternative still on offer. Merge mechanics never escalate; what the user's creations *end up as* can (next bullet).
- The one mid-pass gate kept: **customization survival**. The worker verifies every user creation the update touches against the merged result -- screenshots it actually looks at for visual surfaces, exercised hook points for user-built apps that reach into the system interface's API or state -- and classifies each intact / intact-but-changed / cannot-be-kept. A moved-but-working widget is intact-but-changed: it applies unattended and the results message names it with a restore offer. Cannot-be-kept -- reached only after a genuine attempt to adapt the creation to the new base -- holds the pass with the workspace untouched and asks the user, with the evidence and the concrete options (apply and lose it, skip, or adapt); if the user is away, the pass waits at the gate.
- Rebuild-only findings (a global-dependency bump under user-created code; container-level parameters a running container cannot adopt) no longer pre-clear with the user: the apply proceeds and the results message leads with them as caveats.
- The pre-apply system-interface preview is dropped; the landed workspace is the review surface, with the rollback offer attached to any surface that took nontrivial merge work.
- Beyond that hold, the only confirmation kept is the over-ceiling `--override` (Steps 2/3a): it fires at launch while the user is still present and asks whether to *attempt* a version the driving app cannot support. Terminal outcomes that inherently need a person -- `stuck`, migration-required, an exit-3 emergency -- still end the pass with a user-facing message.

### Failure and rollback

- On any failure past the provisioner step (which alone degrades to the provision-incomplete record above), the script reverts the entire merge as a forward revert commit and restores the pre-apply snapshots: built bundle, root `.venv`, both uv tool environments, `node_modules`. All restores are file copies to their original absolute paths — no network, no `npm`/`uv`, no `mngr` required.
- Globally pinned tools (the `setup_system.sh` tier) roll back by re-running the provisioner from the restored tree, only when the apply had run it; if that re-run fails (e.g. no network), the rollback still counts as recovered and the closing report names the tools left ahead of the tree.
- `env-converge` runs post-success only, so a failed apply never moved apt state.
- The retry path survives every rollback: worker branch, worktree, and report are kept, so a diagnosed retry is a quick re-land. The DRI agent's message offers exactly that.

### Interruption (hard kill) and recovery

- The script writes a marker under `data/.state/` at apply start — DRI agent (from its environment), rollback point, last completed phase, PID — and clears it on every exit path that leaves the tree resolved (a `recover` whose restore fails, and a resumed apply refused on its precondition, deliberately keep it). A concurrent `apply` refuses to start while a live marker exists. A stated invariant of the phase order: the marker is on disk before anything that can disturb the live interface (the restart is the last phase, and every earlier phase works on the side), because the apply mirrors the marker's phase and restamp into `run.json` at the same chokepoint, and the Minds app's stuck-edge probe reads that record over `mngr exec` after an outage begins and declines unattended recovery on finding the apply under way.
- Container restart: bootstrap checks the marker and runs the rollback directly (no agent or UI needed, and dependency-free except for the provisioner re-run noted above), then wakes and messages the DRI agent to verify state and talk to the user.
- Killed without a restart and the DRI agent gone too: a permanent cron entry (written by the bootstrap at each boot, every ~5 minutes) runs `recover` with an only-if-stale guard — marker present, recorded PID dead, older than a grace period — and is a silent no-op in every normal state. It invokes the stdlib-only script directly, never the automations/agent machinery.
- The DRI agent, when alive, simply re-runs the idempotent `apply`; every step tolerates re-entry.

### Memory pressure

- The apply orchestrator is close to OOM-exempt: every agent, chat, and ordinary service is shed before it — losing a build is an ordinary failure the rollback absorbs, but losing the apply mid-motion is the half-applied state this design exists to prevent. Only the authority paths that would repair a failed apply (owner-exec, the terminal) sit below it and would go first.
- Subprocesses inherit that protection by default, which is what the recovery-critical steps need: git operations, snapshot copies and restores, service restarts, the provisioner, `mngr` invocations. During the forward apply, only the genuinely memory-hungry, cleanly recoverable steps — `npm ci` / `npm run build`, the uv installs, the pre-flight boot — are tagged back to the expendable band.
- During rollback and `recover`, nothing is tagged expendable: there is no further rollback to absorb a shed, so every recovery step keeps the orchestrator's protection.

### Skew hardening (independent of the atomic apply)

- The system interface reads mngr config with `strict=False` at its single `load_config` call site, so a settings file written for a newer mngr degrades to a logged warning instead of a 500 on every send — the lockout that made the geebspace incident self-locking. `mngr config set` and all CLI paths keep strict parsing.
- The change classifier treats vendored-mngr *source* changes and `.mngr/settings.toml` changes as restart-requiring, so nothing live keeps reading config newer than its own code after an apply completes.
- The system interface records the tree HEAD it started from and injects a meta tag into the built app shell when the live tree has moved under it; an informational banner tells the user "parts of this workspace were updated but not yet activated" or, from the marker, that an update is part-way through and finishes or undoes itself. The marker-variant copy deliberately does not announce a rollback: the marker is present for the whole of a *healthy* apply too. A third variant reads the apply's emergency record and outranks both — a rollback that could not restore health is the one state here that will not resolve itself, and the one neither other check can see. Acting on any of them stays with the agent.

### Concurrency

- A general "updating workspace" lease (tk chore, like the other flows' leases) is taken by the lead at flow start and held through worker, report audit, and apply — replacing the update-self single-flight ticket check. When the update touches the system interface, the existing `editing service system_interface` lease is additionally taken; that lease is unchanged for non-update SI edits.

### Migration-required updates

- When an update cannot be applied in place (agent judgment, standardized by prose — no mechanical epoch marker), the user-facing message stays high-level: "this update contains fundamental changes that can't be directly applied," followed by exactly two steps — create a new workspace, then message its agent `/migrate-workspace` with this workspace's name. No path tables, no hand-rolled migration plans.
- The refusal also offers the newest in-place-compatible release when one exists ("I can apply X now; Y needs the fresh-workspace migration").

### Cross-version behavior

- An old workspace updating in follows its local prose only through Steps 1–2; the Step 2a handoff stages the target's update-self skill (now containing the apply) and everything from Step 3 on runs the target's flow. The apply therefore tolerates executing against older pre-merge trees (guarded imports, no assumptions about pre-merge layout).
- No compatibility shim for the old `reveal --rollback-to` entry point is needed: its only call sites are in old prose that the handoff supersedes. The update-system-interface local-edit flow always uses its own in-tree copies, so prose and script stay consistent by construction.

## Implementation plan

### `.agents/skills/update-self/scripts/update_self.py` (the bulk of the work)

- New `apply` subcommand (`_cmd_apply`): takes `--merge-ref` (the worker branch / prepared merge commit), `--ff-only` (update-self mode; default is an ordinary merge for update-system-interface), optional `--worker-bundle` (path to the worker's built `static/`), optional `--target-ref` (the release being landed; it is what enables the VERSION_HISTORY.md ledger entry and the post-success `env-converge upgrade`), `--repo-root`. Derives the rollback point (`git rev-parse HEAD` pre-merge) internally.
- New `recover` subcommand (`_cmd_recover`): `--if-stale` (the boot/cron guard: marker present, recorded PID dead, older than `--grace-seconds` → run the rollback; otherwise exit 0 silently), `--no-restart` (the boot path: restore disk state only, since nothing is running yet); bare `recover` for explicit agent-driven rollback of an interrupted apply.
- New data types: an `ApplyMarker` record (DRI agent name from `$MNGR_AGENT_NAME`, rollback sha, last completed phase, PID, timestamps; JSON at `data/.state/update-apply/marker.json`); a phase constant set; a snapshot manifest naming what was copied where.
- New `run-status` subcommand (`start` / `hold <REASON>` / `resume` / `verdict <VERDICT>`): the whole machine-readable status contract between an update-self pass and the Minds app, one `RunStatus` JSON record at `data/.state/update-apply/run.json`. Between start and verdict it carries the run's hold (`CUSTOMIZATION` or `CONFLICT`, with a detail line) and the apply's progress (`apply_phase`, `apply_updated_at`, mirrored from the marker on every restamp and cleared with it), so the app reads everything it shows from this one file and never reads the marker. The lead records the run's start (chat agent name from `$MNGR_AGENT_NAME`, unattended flag) as soon as it holds the updating-workspace lease -- one record per workspace, so a pass refused on a foreign lease must not overwrite the running pass's -- and exactly one terminal verdict (`UPDATED`, `UPDATED_WITH_REBUILD_ITEMS`, `ALREADY_CURRENT`, `NEEDS_RECREATION`, `STUCK`, `REFUSED`, plus a plain-language detail line and optional resulting / in-place-compatible refs) from whichever SKILL.md section ends the pass. The app polls the file over `mngr exec` alongside the run's chat agent — there is no event stream — so a run recorded here is visible to the app whoever launched it, and a new run's `start` supersedes the previous run's record. A verdict is recorded against the agent recording it (`--chat`, else `$MNGR_AGENT_NAME`) rather than against the name already in the file.
- Machinery migrated from `reveal_system_interface.py` (generalized, driven by the existing `classify_path`/`classify_merge` table instead of the SI-only classifier): `Runner` / `HttpClient` / `Spawner` indirections, the copy-aside/restore machinery (`snapshot_targets`, `take_snapshots`, `restore_snapshots`, `discard_snapshots`), the frontend probe (`probe_frontend`, retry-only-non-answers), `_preflight`, `_refresh_backend_dependencies` (the three-env refresh with plugin preservation and PATH-targeted installs), `_restore_tree`/`_commit_rollback`, and the recovery flow.
- New environment snapshots: copy-aside/restore for root `.venv`, both uv tool environments (resolved via the tool receipt / `uv tool dir`), and frontend `node_modules`; restores are plain copies back to the same absolute paths.
- New apply steps: provisioner run (`bash system/scripts/setup_system.sh`) when provisioner-classified paths changed, ordered before any restart; worker-bundle install with live-build fallback and the existing `_assert_bundle_built` check; VERSION_HISTORY.md ledger write (Port of SKILL 5b's Part 1–3 logic: starter recreation, origin-line seeding, idempotent append keyed on note + sha); `env-converge upgrade` post-success with delta summary passthrough.
- Classifier additions: vendored-mngr source (`system/vendor/mngr/**` non-manifest) and `.mngr/settings.toml` become restart-requiring classes.
- OOM banding: band self into a new near-exempt band at process start (apply and recover paths); `as_expendable` tagging applied to `npm ci`/`npm run build`, the uv installs, and the pre-flight boot during the forward apply only — never during rollback/recover. Guarded `oom_priority` import so the staged copy runs on trees that predate the package.
- Idempotence throughout: every phase checks current state before acting (merge already landed → skip; snapshot already taken → reuse; ledger entry present → skip), so re-running `apply` after any interruption is safe.

### `.agents/skills/update-system-interface/scripts/reveal_system_interface.py`

- Shrinks to the `preview` / `unpreview` adapters (and their shared `serve_isolated_instance.py` wiring). The reveal machinery, exit-code contract, and banding move out with the migration above.

### `.agents/skills/update-self/SKILL.md`

- Steps 5b + 5c collapse into the single `apply` invocation (run from the staged skill-at-target copy), with exit-code interpretation and the honest-closing-line guidance.
- The flow is fully unattended (see Expected behavior): the missing-backup go-ahead, the mid-pass conflict escalation, the pre-apply preview, and the approval wait are all removed; 5a becomes a report audit plus a post-apply results message with a real rollback offer.
- Step 1: the "updating workspace" lease replaces the single-flight ticket check; take the SI editing lease additionally when the update touches the system interface.
- New migration-required section: the canonical high-level copy, the two-step `/migrate-workspace` path, and the in-place-compatible-release offer.
- Post-rollback guidance: preserved retry path, DRI recovery composition rules (what to tell the user after an automatic rollback).
- The §2a handoff-contract paragraph updated: Step 3 boundary now includes the apply; staging path unchanged.

### `.agents/skills/update-system-interface/SKILL.md`

- Steps 4–5 replaced by a call to the general apply (`--merge-ref mngr/update-$SLUG`, ordinary merge mode, `--worker-bundle` from the worker's work_dir). Preview flow (Step 3) unchanged.

### `.agents/skills/update-self/references/update-self-worker.md`

- Report contract gains the worker's built-bundle location (for `--worker-bundle`); validation guidance notes its install run pre-warms the uv/npm caches the live refresh reuses.

### `system/apps/system_interface`

- `imbue/system_interface/agent_discovery.py`: `strict=False` at the `load_config` call in `_get_mngr_context` (line ~69), covering all four read paths through it.
- `imbue/system_interface/server.py`: record the tree HEAD at startup; read the apply marker for the "update interrupted" variant; expose the state to the frontend as a meta tag on the built app shell (injected like the existing base-path tag) for an informational banner -- the meta tag is the only surface, since nothing reads a response header.
- Frontend: a small banner component rendering the three staleness messages; informational only.

### `system/services/oom_priority/src/oom_priority/bands.py`

- A new near-exempt band constant for the update apply, sitting above agents/chats/services and below the owner-exec/terminal authority bands.

### Boot and provisioning

- Bootstrap (`system/libs/bootstrap`): at container start, invoke `update_self.py recover --if-stale`; on a recovery, wake and message the DRI agent named in the marker (`mngr start` + `mngr message`, best-effort). It also writes the permanent recovery cron entry (every ~5 minutes, plain `python3` invocation of `recover --if-stale`) at each boot, since `/etc/cron.d` lives on the container rootfs and does not survive the container being recreated.

### Changelogs

- Entries per touched project: `.agents/changelog/`, `system/apps/system_interface/changelog/`, `system/changelog/` (the synthetic `dev` bucket), `system/libs/bootstrap/changelog/`, `system/services/oom_priority/changelog/`, named for the branch.

## Implementation phases

1. **Skew hardening (standalone value)**: `strict=False` in `agent_discovery.py` + classifier additions + their tests. Independently shippable; defuses the geebspace lockout for all subsequent updates.
2. **The general apply**: port and generalize the reveal machinery into `update_self.py apply` (merge modes, env snapshots, provisioner ordering, worker-bundle install, full rollback, banding); shrink `reveal_system_interface.py`; switch `update-system-interface` SKILL.md to it. The SI local-edit flow now runs atomically end to end.
3. **update-self flow rewrite**: SKILL.md 5b/5c collapse, lease changes, migration-required copy, retry guidance; ledger write and `env-converge` move into the script.
4. **Marker and recovery**: marker lifecycle, `recover --if-stale`, bootstrap check + DRI wake, cron entry, staleness header/banner.
5. **Polish and verification**: READMEs, changelog entries, end-to-end passes on a throwaway workspace (including the geebspace repro).

## Testing strategy

- **Unit (recording-runner style, extending the existing `update_self_test.py` patterns)**: apply control flow per change class (frontend-only, backend, vendored-mngr manifest, provisioner, mixed); failure at each phase → full-merge revert + snapshot restore, with exact-command assertions; provisioner runs before any restart; ff-only vs ordinary merge modes; ledger idempotence (re-run appends nothing); marker lifecycle (written first, cleared on every exit path, blocks a concurrent apply); expendable tagging present on forward hungry steps and absent during recovery; classifier additions; env-snapshot copy/restore against real temp dirs.
- **`recover` unit tests**: stale-guard truth table (no marker / live PID / dead PID within grace / dead PID past grace); recovery rollback equals the in-process rollback's end state.
- **Server tests**: unknown config field → agents still listed and a warning logged (strict=False); staleness header appears when HEAD moves and marker variant when a marker exists; banner meta-tag injection.
- **Cross-version**: run the staged copy against a fixture tree lacking `oom_priority` (guarded import) and lacking the marker directory.
- **Manual / end-to-end on a throwaway docker workspace**: a real update-self pass; `kill -9` the apply at each phase and verify boot/cron recovery restores a healthy workspace; a broken-registry rollback (no network) restoring from snapshots; the geebspace repro (stale tool env + new settings) repaired by one apply.

## Open questions

Open questions, review residue, and the incident record for this work are tracked in one place, in the mngr-internal repo: `blueprint/inner-workspace-updates/open-threads.md` (the paired Minds-app plan lives beside it).
