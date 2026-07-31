# Plan: `migrate-workspace` skill

## Refined prompt

> **Spec a new `migrate-workspace` skill for the default-workspace-template: a general main SKILL.md for pulling a user's creations/data/agents from an old Minds workspace into the newly-created one, plus a reference doc for the pre-declutter (minds v0.3.9 and earlier) layout mapping.**
>
> * The skill runs in the newly-created workspace and pulls in; the escape hatch, invoked from the old workspace, creates the new workspace and then tells the user to open it and ask its agent to migrate — a clean, human-visible handoff.
> * The flow reaches the old workspace over a live SSH session and therefore requires it to be online; the backup-export path is covered in prose rather than by the tooling.
> * The user names the source workspace, loosely if they like (a display name, "my old one", "the broken one"), resolved against the workspace listing.
> * The source layout is detected up front: the pre-declutter reference doc loads only when it matches, and a current-layout source runs the general flow with no reference doc.
> * The pre-declutter doc is treated as a one-off for a single historic discontinuity, not the first of a versioned series.
> * Ship one script with subcommands covering the mechanical work (audit, agent recreation, and the rest), named descriptively and not marked crystallized, since this is a hand-written lifecycle skill rather than a crystallized creation.
> * Keep anything the script cannot conclusively enumerate in prose — an incomplete script result must not give the agent false confidence.
> * The script derives the user-vs-template split by diffing the source repo against its own template base, which excludes template-version drift by construction, but labels that list best-effort and the prose directs the agent to verify it against the source tree.
> * Uncommitted work on the source is pinned by committing it there first, after asking the user; the commit is an ordinary one, so if the old workspace auto-pushes to its sync repo that is harmless and arguably desirable.
> * A source with no resolvable template base cannot be migrated automatically; the agent explains why, then offers to migrate everything by hand using the reference map and its own judgment.
> * Take a fresh backup in both the old and the new workspace before any copying begins; if the old workspace has no backups configured, warn the user and get explicit go-ahead rather than stopping.
> * Check whether the target workspace is genuinely fresh: proceed silently if provably untouched, stop and confirm with the user if it already has content, since that raises migration risk.
> * Recommend stopping the old workspace's agents before copying so the source is stable, accept a decline, and fall back to re-syncing anything that shifted during the final verification pass.
> * The migration is single-flight on a tracking ticket like `update-self`, and holds a service-editing lease while it registers apps and restarts services.
> * Every step is idempotent and progress is checkpointed under `data/.tasks/`, so an interrupted run resumes without re-scanning and an expired SSH grant is recoverable.
> * Everything under `data/` comes over, including `anthropic.env`, so migrated integrations keep working by default and the AI audit reviews them afterwards; only the app-minted per-workspace `restic.env` and `cloudflare_tunnel.env` are excluded.
> * Bring every old agent over rather than curating a subset, as a scripted operation: each is recreated via `mngr create --template chat --adopt <session-jsonl>` and left dormant, so its full history renders in a tab and a message revives it.
> * Recreated agents keep their old names and their original `user_created` / `agent_created` labels, and are stopped immediately after creation.
> * All agents are recreated dormant first, and only then is the inventory presented for the user to prune or act on.
> * Every `mngr/<name>` branch is fetched regardless of state, with merged-vs-unmerged reported so the agent can ask the user what to do with each agent and its work.
> * Name collisions auto-suffix for agents, but stop and ask for apps, skills, and ports, which carry wiring that collides for real.
> * The latchkey audit files one batched permission request per scope up front, before the user starts using anything.
> * The AI-integration review rewrites migrated call sites onto the current credential resolver, re-snapshots credentials where unambiguous, and asks the user only where billing is at stake.
> * Edits to template files are ported semantically into their new counterparts and each port is reported.
> * User-made `.mngr/settings.toml` changes follow `update-self`'s taxonomy: live-applicable ones are applied, rebuild-only ones are reported as needing a workspace recreate.
> * Migrated skills get their mechanical path and name references rewritten, then the agent reads each rewritten skill end-to-end to confirm it still makes sense.
> * A migrated app that won't come up gets a bounded repair attempt, and anything unresolved becomes an explicit summary item naming what was tried.
> * Scheduled jobs have their paths rewritten, are installed live, and each job's command is verified to resolve in the new workspace before it is reported as scheduled.
> * Inspiration manifests come over as ordinary user content, and the old ledger's `## Inspirations` and `## Adopted inspirations` entries are merged into the new workspace's so published and adopted history survives.
> * Restoring the old workspace's tab arrangement is out of the tooling's scope; migrated apps are opened as tabs in default positions, chats are not, and arrangement is offered on request.
> * Workspace names are left alone — renaming is the user's business and they can do it from the app.
> * Old GitHub sync is detected and reported, with an offer to run `github-sync` fresh against a new private repo, leaving the old repo untouched as an archive.
> * The skill is reached by description match alone, worded for how users actually phrase it, with no pointers added from `CLAUDE.md`, `docs/`, or the `welcome` skill.
> * The lead does access, backups, the freshness check, and the full inventory/audit — surfacing every question it can up front — then dispatches a worker for the file transfer, path rewriting, re-registration, and agent recreation; the worker inherits the workspace's host-level latchkey grants, so it is self-sufficient and can re-broker an expired SSH grant itself, with a mid-flight question gate back through the lead as `update-self` uses.
> * Verification runs as an automated checklist — apps showing the user's own data, skills loadable, data present, recreated tabs rendering — after which the skill offers to stop or destroy the old workspace while recommending the user check things themselves too.
> * The summary covers what came over, what needs attention and why, and what was deliberately excluded by design, and is written to a file under `data/` so it outlives the conversation.
> * The migration appends an entry under a new `## Migrations` section of `docs/VERSION_HISTORY.md`, matching the existing line shape with the sha being the migration's own commit in the new workspace.
> * A repeat run against the same source is a normal incremental pass: it re-inventories, skips what is already present, and brings over only what is new or missing.
> * The material is split across `SKILL.md`, `references/pre-declutter-layout.md`, and `references/agent-recreation.md`.
> * The reference doc carries the layout/path map, substrate deltas, the "capabilities that didn't exist then" tour, and a per-old-skill replacement table.
> * Bump the four retired-terminology ratchet counts and document the deliberate exception for the pre-rename reference doc.
> * Update the `minds-api` skill's headline workflow to point at this skill.
> * Ship only once the repo gates pass and the script's pure logic has unit tests, and after an end-to-end rehearsal against a real pre-declutter workspace.

## Overview

- **In-place update is impossible across the declutter, so migration replaces it.** The workspace tree was restructured three ways after `minds-v0.3.9` (`mngr/fix-data-layout`, `mngr/declutter-template`, `mngr/creation-rename`) and the container OS moved from Debian 12 bookworm to Debian 13 trixie. `update-self` merges upstream into the live tree and reveals it in place — a path that cannot carry a workspace across a base-image change. The guidance becomes: create a fresh workspace, then have its agent pull the old one in. This skill is that pull.
- **The reorganization is unreleased, so the reference doc is framed by observable layout, not by version.** The declutter landed in merge `9a08e250d` (2026-07-26) and is in no `minds-v*` tag; `minds-v0.3.9` still carries the old root layout. Worse, a 0.3.8 workspace has *no* version marker on disk at all (`VERSION_HISTORY.md` only appeared between 0.3.8 and 0.3.9). So the source is identified by inspection — repo root at `/mngr/code`, a `runtime/` directory, no `data/` — and the version range is context, not the test.
- **One conclusive mechanism carries most of the flow: the baseline diff.** The source repo always has a first-parent template-state marker (`bootstrap` writes `Initial workspace commit` in both versions; `update-self` writes `update-self:` merges), so diffing the source's working tree against *its own* template base yields an exact list of what the user authored in that workspace — and excludes template-version drift by construction, which is what makes auto-porting settings and template-file edits safe. No resolvable base means no automation: the agent explains why and offers a hand-driven migration instead.
- **Everything comes over except a small, named set of things that are per-workspace identity rather than user content.** The exclusions are the old `system-services` agent (a second `is_primary=true` agent makes the workspace ambiguous to discovery), the template machinery (the new workspace has a newer copy), and two app-minted secrets: `restic.env` (a per-workspace R2 bucket keyed by that workspace's own random password) and `cloudflare_tunnel.env` (a tunnel minted per `agent_id`). Copying either would point the new workspace at the old one's resources.
- **Chat recreation is a supported mngr operation, not file surgery.** `mngr create --adopt <path-to-session.jsonl>` copies a session in and resumes it; the system interface renders a tab from that JSONL regardless of process state, and a dormant agent revives on its first message. So every old agent can be recreated faithfully and left stopped — full history, no running processes, no tab clutter.

## Expected behavior

### Getting started

- The user says something like "bring my stuff over from my old workspace", "I made a new mind, move everything", or "my old workspace is broken" — the skill is reached by description match, with no command name to know.
- The agent lists the user's workspaces (including destroyed-but-still-backed-up ones) and asks which to migrate from, accepting a loose reference ("my old one", "the broken one") resolved against that listing.
- Invoked from the *old* workspace instead ("I'd like to move to a new workspace"), the skill creates the new workspace via the minds API and then tells the user to open it and ask its agent to migrate. It does nothing else — the pull-in side owns the rest.
- The flow requires the old workspace to be online, since it works over a brokered SSH session. If the old host cannot start, the agent explains the backup-export alternative in prose rather than automating it.
- A second migration pass already in flight stops the flow with a surfaced explanation, as `update-self` does.

### Before anything is copied

- A fresh backup is taken in both workspaces. If the old workspace has no backups configured, the agent says plainly that there is no restore point and asks for explicit go-ahead rather than stopping — the migration only reads the source, so this is a warning, not a gate.
- The agent checks whether this workspace is genuinely fresh. Provably untouched, it proceeds silently; already carrying content, it stops and confirms, naming the added risk.
- If the source has uncommitted work, the agent asks and then makes an ordinary commit there to pin the state. A resulting auto-push to the old workspace's sync repo is expected and harmless.
- The agent recommends stopping the old workspace's *agents* (not its host — SSH needs that up) so the source cannot shift mid-copy. A decline is accepted; anything that shifts is re-synced during final verification.
- The source's layout is detected. Pre-declutter, the reference map loads; current-layout, the general flow runs with no reference doc.

### Inventory and audit (lead, before dispatch)

- The agent produces one inventory covering: user-authored files from the baseline diff, apps and their ports, skills, scheduled jobs, agents and their branches, latchkey call sites, AI-integration call sites, and hardcoded old paths.
- The baseline-diff list is presented as authoritative-but-verifiable: the agent checks it against the source tree rather than trusting it blindly, and anything the script cannot conclusively enumerate is handled by prose judgment instead of being silently omitted.
- Every question the agent can ask now, it asks now — collisions, unmerged branches, ambiguous ports, billing-relevant AI decisions — so the user is not interrupted repeatedly later.
- One batched latchkey permission request per scope is filed up front, before the user starts using anything. Grants are keyed to the *host*, so the new workspace starts from a deny-all baseline regardless of what the old one had.

### Migration (worker)

- A worker performs the transfer, path rewriting, re-registration, and agent recreation. It inherits the workspace's host-level latchkey grants, so it is self-sufficient and can re-broker an expired SSH grant itself; genuinely undecidable cases surface through the lead's question gate.
- Every step is idempotent and checkpointed under `data/.tasks/`, so an interrupted run resumes without re-scanning.
- **Data:** everything under the source's `data/` equivalent arrives at its new location, including `anthropic.env` so migrated integrations keep working by default. Only `restic.env` and `cloudflare_tunnel.env` are excluded.
- **Apps:** each lands under `system/apps/<package>/`, is registered in the root manifest and `supervisord.conf`, and re-registers its port through `forward_port.py` rather than inheriting the old registry file. One that will not come up gets a bounded repair attempt; whatever is left becomes an explicit summary item naming what was tried.
- **Skills:** mechanical path and name references are rewritten automatically, then the agent reads each rewritten skill end-to-end to confirm it still means what it meant.
- **Template-file edits:** ported semantically into their new counterparts — `CLAUDE.md` additions appended to the new `CLAUDE.md`, settings keys set in the new settings file, supervisord program blocks re-added — with each port reported.
- **`.mngr/settings.toml` edits:** follow `update-self`'s taxonomy. Live-applicable changes are applied; rebuild-only ones (container build/launch parameters) are reported as needing a workspace recreate.
- **Scheduled jobs:** paths rewritten, installed live into `/etc/cron.d`, and each command verified to actually resolve here before the job is reported as scheduled.
- **Agents:** every old agent except `system-services` is recreated dormant, under its old name and original `user_created` / `agent_created` label, with its session adopted so its tab renders full history. New agent ids are minted — the old ones may still be live on the old host.
- **Branches:** every `mngr/<name>` branch is fetched, with merged-vs-unmerged classified by whether its tip is an ancestor of the source's checked-out branch. Unmerged branches carry work that is not in the migrated tree, so they are called out for a decision.
- **Collisions:** agents auto-suffix and the rename is reported; apps, skills, and ports stop and ask, because their wiring collides for real.
- **Inspirations:** manifests come over as ordinary user content, and the old ledger's `## Inspirations` / `## Adopted inspirations` entries merge into this workspace's ledger.

### Finishing

- The agent presents the full agent inventory — names, last activity, merged/unmerged branch state — for the user to prune or act on, now that recreation has already happened cheaply and reversibly.
- Verification runs as an automated checklist: each migrated app opens showing the user's own data, each skill loads, data is present, each recreated tab renders. Migrated apps are opened as tabs in default positions; chats are not, and reproducing the old arrangement is offered on request rather than automated.
- The AI-integration review rewrites migrated call sites onto the current credential resolver, re-snapshots credentials where the answer is unambiguous, and asks the user only where billing is at stake — a copied key in a subscription-auth workspace silently bills full API rates, so that case is always a question.
- Old GitHub sync is reported, with an offer to run `github-sync` fresh against a *new* private repo, leaving the old repo untouched as an archive.
- The summary names what came over, what needs attention and why, and what was deliberately excluded by design — including the two secret files, the old services agent, and the template machinery — and is written to a file under `data/` so it outlives the conversation.
- The skill then offers to stop or destroy the old workspace, recommending the user check things over themselves first rather than relying on the checklist alone. Destroy additionally requires explicit confirmation and a fresh backup.
- An entry is appended under a new `## Migrations` section of `docs/VERSION_HISTORY.md`, matching the existing line shape, with the sha being the migration's own commit in this workspace.
- Workspace names are left alone.
- Running the skill again against the same source is a normal incremental pass: re-inventory, skip what is already present, bring over only what is new or missing.

## Changes

### New skill

- `.agents/skills/migrate-workspace/SKILL.md` — the general, version-agnostic flow: source identification, preconditions, quiescence, inventory/audit, worker dispatch and gate proxying, verification, summary, ledger entry, old-workspace disposition, and the escape-hatch section for being invoked from the old side. Description worded for how users actually phrase the request, and for the escape-hatch phrasing from the old side. Body must stay within the 500-line cap `validate_skill.py` enforces.
- `.agents/skills/migrate-workspace/references/pre-declutter-layout.md` — the old→new map for `minds-v0.3.9` and earlier: the detection test, the path table (`/mngr/code` → `/home/user/workspace`, `/mngr` → `/home/user/.mngr`, `/mngr/worktree/` → `/home/user/worktrees/`, `runtime/memory/` → `data/memories/`, `runtime/tickets/` → `data/.tickets/`, `runtime/oom_priority/` → `data/.state/oom_priority/`, `runtime/<app>/` → `data/.apps/<app>/`, `uploads/` → `data/uploads/`, `libs/<pkg>` → `system/apps/<pkg>` or `system/services/<name>`, `scripts/` → `system/scripts/`, `supervisord.conf` → `system/supervisord.conf`, `parent.toml` → `system/config/parent.toml`, `skills-lock.json` → `.agents/skills-lock.json`, and the rest), the substrate deltas (bookworm→trixie, snapshot-pinned apt and `env.d` units replacing ad-hoc `apt-get`, Fortress replacing stock Playwright Chromium, uv member globs, `deferred-install`→`env-converge`, the retired `runtime-sync` branch), a per-old-skill replacement table (`build-web-service`→`build-app`, `update-service`→`update-app`, `crystallize`/`update`/`heal-artifact`→`-creation`, worker reference renames), the vocabulary rename, and a "capabilities that didn't exist then" tour (scheduled tasks and the Caretaker, inspirations, layout ops, `data/.apps`). Cites `claude_p.py` at its real location under the `use-ai-integration` skill, not the path the current skill prose gives.
- `.agents/skills/migrate-workspace/references/agent-recreation.md` — the version-agnostic session-adoption mechanics: enumerating agents from `agents/*/data.json` and `preserved/`, mapping each to its sessions via `claude_session_id_history` (load-bearing, since minds chat agents share one `CLAUDE_CONFIG_DIR` and all sessions sit in one `projects/` tree), the `mngr create --template chat --transfer none --adopt <jsonl> --label ...` invocation, stopping each agent after creation, the encoded-project-dir detail (`/mngr/code` → `-mngr-code` vs `/home/user/workspace` → `-home-user-workspace`), and why `system-services` is excluded.
- `.agents/skills/migrate-workspace/scripts/<descriptive-name>.py` — one script, subcommands for the mechanical work: source inventory, baseline diff, latchkey and AI call-site scans, hardcoded-path scan, port and job enumeration, branch merged/unmerged classification, and agent recreation. Not `run.py`, not marked `metadata.crystallized: true`.
- A unit test beside the script covering its pure logic: branch-merged classification, path mapping, audit pattern matching, and agent→session resolution.

### Existing files

- `.agents/skills/minds-api/SKILL.md` — its "Headline workflow: migrate an old workspace into a fresh one" section points at this skill rather than carrying a thinner version of the same flow.
- `docs/VERSION_HISTORY.md` — a `## Migrations` section is introduced (written by the skill at migration time; the starter heredoc that `update-self` owns gains the section so a recreated file has it).
- `system/test_meta_ratchets.py` — the four retired-terminology counts (`creations/`, `artifact`, `web service`, `application`) are bumped for the reference doc's deliberate naming of retired terms, with a comment explaining that a pre-rename migration map must name them. Counts are re-recorded via `uv run pytest --inline-snapshot=trim system/test_meta_ratchets.py`.
- `.agents/changelog/gabriel-lemon-polecat.md` and `system/changelog/gabriel-lemon-polecat.md` — the two per-project entries the changelog gate requires (the skill tree maps to the `agents` bucket; the ratchet file and this blueprint doc map to `dev`).

### Deliberately not changed

- No pointer from `CLAUDE.md`, `docs/`, `docs/README.md`, or the `welcome` skill — the description is the whole discovery surface.
- No layout-era registry or version-detection machinery beyond the single inline check; the pre-declutter doc is a one-off.
- No tab-arrangement translation, and no workspace rename.

### Acceptance criteria

- `uv run .agents/shared/scripts/validate_skill.py .agents/skills/migrate-workspace` passes.
- `.agents/shared/scripts/test_skill_mngr_references.py` passes — every `mngr <subcommand>` in the new prose names a real command.
- `system/scripts/check_changelog_entries.py` passes with both entries present.
- `system/test_meta_ratchets.py` passes at the re-recorded counts.
- The script's unit tests pass, and the full suites for the touched projects pass.
- An end-to-end rehearsal against a real pre-declutter workspace completes: creations, data, scheduled jobs, and agents arrive; recreated tabs render their history; the summary names every excluded and unresolved item. Rehearsal findings are folded back into the reference doc as known gotchas.
