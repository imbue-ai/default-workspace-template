---
name: migrate-workspace
description: "Bring everything from another (older, broken, or replaced) workspace of the user's into this one -- their apps, skills, documents, data, scheduled automations, and every past chat. Use when the user says anything like 'move my stuff over from my old workspace', 'I made a new mind, bring everything across', 'my old workspace is broken, start me fresh', 'import my other mind', or -- asked from the OLD side -- 'I'd like to move to a new workspace'. Requires the other workspace to be startable, since the transfer runs over a live connection to it."
compatibility: Requires latchkey (the minds-api gateway) plus ssh/ssh-keygen for the live session, and mngr (vendored) for recreating the old chats.
---

# Migrating another workspace into this one

The workspace tree was restructured after `minds-v0.3.9` and the container image
moved from Debian 12 to Debian 13, so an old workspace **cannot** update itself
across that boundary -- `update-self` merges upstream into a live tree, which no
base-image change survives. Migration replaces it: create a fresh workspace, then
have its agent pull the old one in. **You are that agent, and this skill is that
pull.**

One mechanism carries most of the flow: the **baseline diff**. The source repo
always has a first-parent template-state marker (`bootstrap` writes `Initial
workspace commit`; `update-self` writes `update-self:` merges), so diffing the
source's working tree against *its own* template base yields an exact list of
what the user authored there -- and excludes template-version drift by
construction. That is what makes auto-porting settings and template-file edits
safe. **No resolvable base means no automation** (Step 4).

You are the **lead**: get access, take backups, check this workspace is fresh,
produce the whole inventory and audit, and surface every question you can *up
front*. Then dispatch a **worker** for the transfer, path rewriting,
re-registration, and agent recreation, proxy its one gate, verify, and write the
summary. Every mechanical step lives in
`.agents/skills/migrate-workspace/scripts/migrate_workspace.py`; its results are
the raw material for your judgement, not a substitute for it. **Where the script
reports something ambiguous or incomplete, that is a real question -- never treat
an empty or partial result as "nothing there".**

## 0. If the user asked you from the OLD workspace

If you are reading this in the workspace being *left* ("I'd like to move to a new
workspace"), you do exactly two things and stop:

1. Create the fresh workspace via the `minds-api` skill (`POST
   /api/v1/workspaces` with the template `git_url`, then poll
   `operations/create/<op>` until `DONE`). Leave every `backup_*` field unset.
2. Tell the user plainly: the new workspace is ready, open it, and ask its agent
   to bring everything over from this one. Name this workspace so they can say
   which.

Do not copy anything yourself. The pull-in side owns the rest, and a
human-visible handoff is the point.

## 1. Identify the source

List the user's workspaces and ask which one to migrate from. Accept a loose
reference ("my old one", "the broken one") and resolve it against the listing --
match on `name`, and use `host_state` and the version to disambiguate.

```bash
latchkey curl http://latchkey-self.invalid/minds-api-proxy/api/v1/workspaces
```

The listing includes destroyed-but-still-backed-up workspaces, so an old
workspace is findable even after its host is gone. Note the source's `agent_id`
(call it `OLD`) and confirm the choice with the user before doing anything else.

**The flow needs the source online.** It works over a live SSH session, so if the
old host will not start, say so and explain the alternative in plain terms: minds
can export that workspace's newest backup snapshot as a zip
(`POST .../<OLD>/backups/<snapshot_id>/export`), and the two of you can work
through its contents by hand. Do not try to drive a restore-from-backup migration
through this skill's tooling; it is built around a live source.

## 2. Get access, in one batched request

Grants are keyed to the **host**, so this workspace starts from a deny-all
baseline no matter what the old one had. File **one** permission request covering
every verb the whole pass needs, before the user starts using anything -- per the
`minds-api` skill's `type: "workspace"` request:

```bash
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"$MNGR_AGENT_ID"'", "type": "workspace",
       "payload": {"permissions": ["minds-workspaces-ssh", "minds-workspaces-lifecycle",
                                   "minds-workspaces-backups-export", "minds-workspaces-destroy"],
                   "target_workspace_id": "<OLD>"},
       "rationale": "Bring <name> across into this workspace: read it over SSH, start it if it is stopped, and (only if you ask me to at the end) stop or remove it."}'
```

Wait for the approval message. Then start the source if it is stopped
(`POST .../<OLD>/start`) and broker the SSH session:

```bash
ssh-keygen -t ed25519 -N '' -f /tmp/mind_key
CONN=$(latchkey curl -XPOST http://latchkey-self.invalid/minds-api-proxy/api/v1/workspaces/<OLD>/ssh \
  -H 'Content-Type: application/json' \
  -d '{"public_key": "'"$(cat /tmp/mind_key.pub)"'", "requester_workspace_id": "'"$MNGR_AGENT_ID"'"}')
echo "$CONN"
```

Keep the `host`, `port`, and `user` from `$CONN` -- every `migrate_workspace.py`
call below takes them as `--ssh-host` / `--ssh-port` / `--ssh-user`. The grant is
time-limited: any script call that exits **3** means it lapsed (or the host went
away), and the fix is to re-request the grant and retry, not to give up.

## 3. Preconditions

**Single-flight.** One migration at a time (the worker name, branch, and runtime
dir are fixed). Check for a live one, and take over an abandoned one per
`.agents/shared/references/harden-contention.md`:

```bash
tk ready > /tmp/migrate-inflight.txt
grep "migrate-workspace" /tmp/migrate-inflight.txt
```

**Back up both sides.** Here, and on the source over SSH:

```bash
uv run host-backup-now
ssh -i /tmp/mind_key -p <port> <user>@<host> 'cd <source-repo-root> && uv run host-backup-now'
```

Confirm each prints `restic_backup_succeeded`. If the *source* reports
`tick_skipped_due_to_missing_secrets`, it has no restore point: tell the user
plainly and get their explicit go-ahead. This is a warning, not a gate -- the
migration only ever reads the source.

**Is this workspace actually fresh?** Compare its own tree against its own
template base. If nothing but the template base is there, proceed silently. If it
already carries content, **stop and confirm**, naming the risk: migrated files can
collide with what is already here, and collisions in app wiring are not
auto-resolved.

```bash
git log --first-parent --format='%H %s' HEAD
git diff --name-status "$(git log --first-parent --format='%H %s' HEAD \
    | awk '$0 ~ /^[^ ]+ update-self:/ || $0 ~ /^[^ ]+ Initial workspace commit$/ {print $1; exit}')"
```

**Pin the source's state.** If the source has uncommitted work, ask, then commit
it *there* as an ordinary commit so the baseline diff sees a stable tree. An
auto-push to the old workspace's own sync repo is expected and harmless.

**Recommend quiescence.** Ask the user to let you stop the source's *agents* (not
its host -- SSH needs that up) so the tree cannot shift mid-copy:
`ssh ... 'mngr stop <agent>'` for each non-primary agent. A decline is fine;
anything that shifts is re-synced during verification (Step 8).

## 4. Detect the layout, then resolve the baseline

```bash
uv run .agents/skills/migrate-workspace/scripts/migrate_workspace.py detect-layout \
    --ssh-host <host> --ssh-port <port> --ssh-user <user>
```

- **`current`** -- run the general flow below with no reference doc.
- **`pre-declutter`** -- **load
  [references/pre-declutter-layout.md](references/pre-declutter-layout.md)** and
  keep it open for the rest of the pass. It carries the old-to-new path map, the
  substrate deltas, the per-old-skill replacement table, and a tour of the
  capabilities that did not exist then.
- **`unknown`** -- stop and show the user the reported `reason`. Do not migrate
  from a tree you cannot describe.

Then resolve what the user actually authored there, passing the `repo_root` and
`layout` the detection reported:

```bash
uv run .agents/skills/migrate-workspace/scripts/migrate_workspace.py baseline-diff \
    --ssh-host <host> --ssh-port <port> --ssh-user <user> \
    --repo-root <repo_root> --layout <layout>
```

**Exit 4 means there is no resolvable template base**, so nothing can be derived
automatically. Do not improvise a substitute. Explain plainly why -- that repo was
not created by the workspace bootstrap, so there is no "what shipped" line to
measure the user's own work against -- and then **offer to migrate everything by
hand**, using the reference map and your own reading of the source tree, deciding
each file with the user. That is a legitimate outcome, not a failure.

The result is **authoritative but verifiable**. Check it against the source tree
rather than trusting it blindly: walk the source's own directories for anything
the diff would not see (files the user created outside the repo, and gitignored
content under the source's data tree), and resolve every entry the script flagged
`is_ambiguous` -- those are places the old tree collapsed a distinction the new
one makes, and only reading the file settles it.

## 5. Inventory and audit -- ask everything now

Produce **one** inventory before dispatching anything. Every question you can ask
now, ask now: the user should not be interrupted repeatedly later.

```bash
S="--ssh-host <host> --ssh-port <port> --ssh-user <user>"
M=".agents/skills/migrate-workspace/scripts/migrate_workspace.py"
uv run $M list-agents        $S --host-dir <host_dir>
uv run $M classify-branches  $S --repo-root <repo_root>
uv run $M list-ports         $S --repo-root <repo_root>
uv run $M list-jobs          $S --repo-root <repo_root>
uv run $M audit-scan         $S --paths-from /tmp/baseline-paths.txt
```

(Write `/tmp/baseline-paths.txt` from the baseline diff's entries, prefixed with
the source's `repo_root`. Each result is checkpointed under
`data/.tasks/migrate-workspace/`, so a re-run serves the cached scan; pass
`--refresh` to re-read the source.)

Read each result's `caveat` field -- it names what that scan does *not* settle.
Then bring the whole picture to the user in one message:

- **Their creations**, in plain terms: the apps, skills, documents, and data
  coming across, and anything you could not classify.
- **Collisions.** Agent names auto-suffix and are merely reported. **Apps,
  skills, and ports stop and ask** -- an app's program name and listening port
  are real wiring, and two apps cannot share either.
- **Unmerged branches.** Each carries work that is *not* in the migrated tree.
  Ask what to do with each one and its agent.
- **Billing-relevant AI decisions** (Step 8's review) and any scheduled job whose
  purpose is unclear.

**File the migrated call sites' latchkey grants now, not later.** The `latchkey`
findings name which third-party services the user's own creations reach. Grants
are keyed to the host, so none of the old workspace's carried over. File **one
batched permission request per scope** here -- before the user starts using
anything -- rather than letting each migrated app hit a denial the first time they
open it. Use the `latchkey` skill's `type: "predefined"` request, one call per
scope, all of them back-to-back, with a rationale naming the creation that needs
it.

## 6. Dispatch the worker

Open the tracking ticket, write the task file, launch, and background-poll.

```bash
mkdir -p data/.tasks/migrate-workspace
tk create "migrate-workspace" -t task \
    --acceptance "data, creations, jobs, and agents transferred; paths rewritten; apps and jobs registered; verified"
```

Note the ticket id, then start it (its own tool call, nothing chained):

```bash
tk start <ticket-id>
```

Write the task file with the two-heredoc form -- an **unquoted** frontmatter block
so the variables expand, then a **quoted** body so its backticks stay literal:

```bash
{
cat << FRONTMATTER_EOF
---
lead_agent: $MNGR_AGENT_NAME
finish_report_path: data/.tasks/migrate-workspace/reports/report.md
source_repo_root: <repo_root>
source_host_dir: <host_dir>
source_layout: <layout>
ssh_dest: <user>@<host>:<port>
FRONTMATTER_EOF
cat << 'BODY_EOF'
---

# Task: migrate the old workspace in

## What to do
Follow `.agents/skills/migrate-workspace/SKILL.md` Step 7 (the worker's half) end
to end, plus `references/agent-recreation.md` for the chats. For a
`pre-declutter` source_layout, `references/pre-declutter-layout.md` is the
old-to-new map -- read it first. The lead has already taken backups, resolved the
baseline diff, and settled every collision and branch decision with the user; its
answers are in `data/.tasks/migrate-workspace/decisions.md`. The brokered SSH key
is at /tmp/mind_key; you may re-request the grant yourself if it lapses (script
exit 3).

## Reporting back
Per `.agents/shared/references/worker-reporting.md`. Valid `name:` values:
`question` (a genuinely undecidable case), `done` / `stuck` (terminal).
Substitutions: `<TASK_FILE_GLOB>` -> `data/.tasks/migrate-workspace/task.md`;
`<RUNTIME_REPORTS_DIR>` -> `data/.tasks/migrate-workspace/reports`.
BODY_EOF
} > data/.tasks/migrate-workspace/task.md
```

Write the user's answers to `data/.tasks/migrate-workspace/decisions.md` before
launching, then launch with the plain `worker` template and background-poll
(`run_in_background: true`), re-arming per
`.agents/shared/references/lead-proxy.md`:

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name migrate-workspace --template worker \
    --runtime-dir data/.tasks/migrate-workspace/ --task-file data/.tasks/migrate-workspace/task.md

uv run .agents/skills/launch-task/scripts/create_worker.py await \
    --name migrate-workspace --task-file data/.tasks/migrate-workspace/task.md --timeout 90m
```

## 7. The worker's half

Every step is idempotent and checkpointed under `data/.tasks/migrate-workspace/`,
so an interrupted run resumes without re-scanning. Take the
`editing service <name>` lease (see `update-app`'s "One editor at a time") while
registering apps and restarting services, and release it when done.

**Data.** Everything under the source's data tree arrives at its new location --
**including `data/.secrets/anthropic.env`**, so migrated integrations keep working
by default. Exactly two files are excluded, because they are per-workspace
identity rather than user content: `restic.env` (an R2 bucket keyed by *that*
workspace's own random password) and `cloudflare_tunnel.env` (a tunnel minted per
`agent_id`). Copying either would point this workspace at the old one's
resources. Use `rsync` over the SSH session with an `--exclude` for each.

**Creations.** Each app lands under `system/apps/<package>/`, is added to the root
`pyproject.toml`, gets a `[program:<name>]` block in `system/supervisord.conf`
that runs `system/scripts/forward_port.py` before its own start command, and
re-registers its port that way -- never by copying the old registry file, which is
runtime state. Then `uv sync --all-packages` and
`supervisorctl reread && supervisorctl update`. An app that will not come up gets
a **bounded** repair attempt (read its stderr log, fix the obvious break, retry
once or twice); whatever is still broken becomes an explicit summary item naming
what you tried.

**Skills.** Rewrite the mechanical references, then **read each rewritten skill
end to end** to confirm it still means what it meant:

```bash
uv run .agents/skills/migrate-workspace/scripts/migrate_workspace.py rewrite-refs \
    --paths-from /tmp/migrated-skill-files.txt
```

The report lists every substitution. The rewriter deliberately leaves the
ambiguous legacy prefixes alone -- resolve those by reading.

**Template-file edits.** Port them *semantically* into their new counterparts and
report each port: `CLAUDE.md` additions appended to this `CLAUDE.md`, settings
keys set in the new settings file, supervisord program blocks re-added. Never
overwrite a template file with the old one wholesale.

**`.mngr/settings.toml` edits.** Follow `update-self`'s taxonomy. Live-applicable
changes (env vars, agent behavior, `settings_overrides`) are applied and take
effect on the next process start. Rebuild-only ones -- a `[create_templates.*]` /
`[providers.*]` `build_arg`, `start_arg`, or runtime flag that an already-running
container cannot adopt -- are **reported as needing a workspace recreate**, never
implied to be live.

**Scheduled jobs.** `list-jobs` already rewrote each command's paths. Write the
durable copy under `data/.state/cron.d/<job-name>`, install it live with
`install -m 0644 ... /etc/cron.d/<job-name>`, and **verify each command actually
resolves here** (the binary/script exists and runs) before reporting the job as
scheduled. A rewritten path can still name a script that was never migrated.

**Agents.** Stage the session JSONLs off the source, then recreate every agent
except the source's primary -- dormant, under its old name, with its original
creation label:

```bash
uv run .agents/skills/migrate-workspace/scripts/migrate_workspace.py recreate-agents \
    --agents-json data/.tasks/migrate-workspace/agents.json \
    --sessions-dir data/.tasks/migrate-workspace/sessions
```

See [references/agent-recreation.md](references/agent-recreation.md) for the
staging, the encoded-project-dir detail, and why the primary is excluded.

**Branches.** Fetch every `mngr/<name>` branch regardless of state, so no commits
are lost, and carry the merged/unmerged classification into your report.

**Inspirations.** The source's `inspiration-*.md` manifests and their `.svg`
thumbnails come over as ordinary user content, and the old ledger's
`## Inspirations` and `## Adopted inspirations` entries are **merged into** this
workspace's `docs/VERSION_HISTORY.md` -- append-only, existing lines copied
through verbatim -- so published and adopted history survives. Do **not** carry
the old `## Workspace` lines: those record where the *old* workspace came from.

Commit on `mngr/migrate-workspace` and report `done`.

## 8. Handle the report, then verify

Proxy a `question` gate per `.agents/shared/references/lead-proxy.md` (worker
`migrate-workspace`, branch `mngr/migrate-workspace`, reports dir
`data/.tasks/migrate-workspace/reports/`): escalate genuine decisions about the
user's intent to the user, relay the answer with `mngr message`, consume the
report, re-arm. On `stuck` or a dead-worker timeout, follow
`.agents/skills/launch-task/references/worker-failure.md` -- nothing has been
applied here, and the source is untouched either way.

On `done`, merge the branch, then work the checklist. Each item is a check, not
an assumption:

- **Every migrated app opens and shows the user's own data** -- not an empty
  state. Open it and look.
- **Every migrated skill loads**:
  `uv run .agents/shared/scripts/validate_skill.py .agents/skills/<name>`.
- **The data is present** at its new locations, including the secrets file that
  was supposed to come over.
- **Every recreated tab renders its history.**
- **Nothing shifted on the source** during the copy (if the user declined
  quiescence): re-run the baseline diff with `--refresh` and re-sync anything new.

Open the migrated **apps** as tabs in default positions
(`for L in desktop mobile; do python3 system/scripts/layout.py open --layout "$L" service:<name>; done`).
Do **not** open the recreated chats -- there can be many, and a wall of tabs is
worse than none. Reproducing the old workspace's arrangement is out of scope;
offer to lay things out if the user asks.

**The AI-integration review.** For each `ai` finding: rewrite the call site onto
the current credential resolver (`read_workspace_ai_credentials()` in
`.agents/skills/use-ai-integration/scripts/claude_p.py`), and re-snapshot the
credential where the answer is unambiguous. **Ask the user whenever billing is at
stake** -- a copied API key in a subscription-auth workspace silently bills full
API rates, so that case is always a question. See the `use-ai-integration` skill.

**Latchkey.** Step 5 already filed the migrated call sites' grants. Confirm each
one landed, and only re-request a scope the audit genuinely missed -- never
re-ask for a permission you have already been told was granted.

**Old GitHub sync.** If the source had it enabled (a `github_sync.toml` and an
`origin` pointing at a private repo), report that plainly and offer to run
`github-sync` fresh against a **new** private repo here, leaving the old repo
untouched as an archive. Never repoint this workspace at the old repo.

## 9. Close out

**Write the summary to a file** so it outlives the conversation -- and then tell
the user the same thing in plain language:

```bash
mkdir -p data/documents && $EDITOR data/documents/migration-summary.md   # or just write the file
```

It names three things: **what came over**, **what needs attention and why** (each
unresolved app, each rebuild-only setting, each unmerged branch), and **what was
deliberately excluded by design** -- the two app-minted secret files, the old
workspace's services agent, and the template machinery, each with its one-line
reason. Never let an exclusion read as an oversight.

**Record it in the ledger.** Append one line under a `## Migrations` section of
`docs/VERSION_HISTORY.md` (create the section after `## Workspace` if absent),
matching the existing line shape -- note padded to width 26, ending in a 7-char
sha. Here the sha is **this migration's own commit in this workspace**. Then stage
that one file **by name** and commit it; never `git add -A`.

```
- <today, YYYY-MM-DD>  migrated from <name>       <7-char sha>
```

**Then offer the old workspace's disposition** -- and recommend the user look
things over themselves first rather than relying on the checklist alone:

- **Stop it** (`POST .../<OLD>/stop`) -- the safe default; everything stays
  recoverable.
- **Destroy it** (`POST .../<OLD>/destroy`) -- only on explicit confirmation, and
  only after a fresh backup you have confirmed succeeded.

Leave the workspace **names** alone -- renaming is the user's business and they can
do it from the app.

Finally, tear down per `launch-task`'s conventions: consume the terminal report
into `data/.tasks/migrate-workspace/reports/consumed/`, destroy the worker, and
close the ticket last (its own tool call).

## Running it again against the same source

A repeat pass is ordinary and incremental, not a restart: re-inventory with
`--refresh`, skip what is already present (`recreate-agents` skips agents already
in its ledger; the transfer skips files already in place), and bring over only
what is new or missing. Say what the second pass actually added.
