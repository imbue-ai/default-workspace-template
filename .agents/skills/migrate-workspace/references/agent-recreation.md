# Recreating the old workspace's chats here

Bringing chats across is a **supported mngr operation, not file surgery**:
`mngr create --adopt <session.jsonl>` copies a session into a newly created agent
and resumes that conversation. The system interface renders a tab from that JSONL
regardless of process state, and a dormant agent revives on its first message. So
every old chat can be recreated faithfully and left **stopped** -- full history, no
running processes, no tab clutter.

Bring **every** agent over rather than curating a subset. Recreation is cheap and
reversible; pruning is the user's call, and they make it *after* seeing the
inventory (the skill's Step 8), not before. This is version-agnostic: it applies
whether the source is pre-declutter or current-layout.

## 1. Enumerate the agents

`migrate_workspace.py list-agents --host-dir <host_dir>` does this in one batched
remote read. What it reads, and why:

- **`<host_dir>/agents/<agent_id>/data.json`** -- every agent still present on the
  source, running *or* stopped-but-not-destroyed. Note the directory is keyed by
  **agent id**, not name; `data.json` carries the `name`, `id`, and `labels`.
- **`<host_dir>/preserved/<agent_name>--<agent_id>/`** -- agents that were
  destroyed. mngr preserves their session transcripts and history file on destroy
  precisely so they stay recoverable, so a destroyed chat the user still cares
  about is migratable.
- **`<host_dir>/{agents,preserved}/*/claude_session_id_history`** -- the
  append-only `"<session_id> <source>"` log a SessionStart hook writes, oldest
  first.

`<host_dir>` is `/home/user/.mngr` on a current-layout source and `/mngr` on a
pre-declutter one; `detect-layout` reports it.

An agent whose `data.json` is missing or unparseable comes back in `unreadable`
rather than being skipped silently -- a half-written state dir is worth mentioning,
not worth guessing about. An agent with **no** session file has no transcript to
adopt; recreating it produces an empty tab, so ask the user whether they want it
at all.

## 2. Why the history file is load-bearing

Every minds chat agent shares the primary agent's `CLAUDE_CONFIG_DIR` (the
template sets `isolate_local_config_dir = false` on the `claude` type and `true`
only on `main`), so **all** of their sessions sit in **one** `projects/` tree with
nothing but the session id to tell them apart. There is no per-agent directory to
read. `claude_session_id_history` is therefore the only thing that says which
sessions belonged to which agent.

That tree is under the *primary* agent's state dir:

```
<host_dir>/agents/<primary_agent_id>/plugin/claude/anthropic/projects/<encoded-work-dir>/<session_id>.jsonl
```

`resolve_agent_sessions` matches history ids to those files by filename stem,
preserving history order and dropping duplicates (a `/clear` or `/compact` appends
a new id). Order matters: with several `--adopt` flags, **every** named session is
copied in and the **last one** is resumed on startup -- so history order puts the
agent's most recent conversation in front of the user.

Ids in the history with no file on disk come back as `unresolved_session_ids`
(an aborted session, or one whose transcript was pruned). Report them; do not
treat their absence as meaning the agent had no history.

### The encoded project directory

Claude Code files per-project data under `projects/<encoded-path>/`, where the
encoding keeps only ASCII alphanumerics and `-` and maps everything else to `-`.
So the same workspace encodes differently across the layout change:

| Work dir | Encoded project dir |
|---|---|
| `/mngr/code` | `-mngr-code` |
| `/home/user/workspace` | `-home-user-workspace` |

You do **not** re-encode by hand: `mngr create --adopt` re-files the adopted JSONL
under the *destination's* encoding as part of the create, and also fixes up the
resume pointer. The encoding matters for two things only -- **finding** the source
files (a pre-declutter source's sessions are under `-mngr-code`, which is why the
enumeration globs `*/projects/*` rather than assuming a directory name), and
recognizing that a session copied in by plain `rsync` into the wrong project dir
would be invisible to Claude on resume. Always go through `--adopt`.

## 3. Stage the session files locally

`--adopt` resolves a **local** path, so the JSONLs have to be here first. Copy the
whole set in one pass, then hand the directory to `recreate-agents`:

```bash
mkdir -p data/.tasks/migrate-workspace/sessions
rsync -a -e "ssh -i /tmp/mind_key -p <port> -o StrictHostKeyChecking=no" \
    --include='*/' --include='*.jsonl' --exclude='*' --prune-empty-dirs \
    "<user>@<host>:<host_dir>/agents/" data/.tasks/migrate-workspace/sessions-raw/
find data/.tasks/migrate-workspace/sessions-raw -name '*.jsonl' \
    -exec cp -n {} data/.tasks/migrate-workspace/sessions/ \;
```

The flatten into one `sessions/` directory is deliberate: session ids are UUIDs, so
the basenames cannot collide, and `recreate-agents` looks each remote path's
basename up there. Repeat for `<host_dir>/preserved/` if any destroyed agent is
being brought over. `cp -n` keeps a re-run from overwriting an already-staged file.

## 4. Recreate, dormant

```bash
uv run .agents/skills/migrate-workspace/scripts/migrate_workspace.py recreate-agents \
    --agents-json data/.tasks/migrate-workspace/agents.json \
    --sessions-dir data/.tasks/migrate-workspace/sessions
```

Per agent it runs, then immediately `mngr stop <name>`:

```bash
mngr create <name> --template chat --transfer none --no-connect \
    --adopt <session-1.jsonl> [--adopt <session-2.jsonl> ...] \
    --label user_created=true [--label project=<project>]
```

Each flag is doing a specific job:

- **`--template chat`** is how the system interface creates a chat agent, so the
  recreated agent gets a tab and the user-facing output style.
- **`--transfer none`** runs it in place in this workspace's checkout, sharing the
  primary agent's work dir and Claude config dir -- exactly like a natively created
  chat. A worktree would isolate it from the migrated tree, which is the opposite
  of what a chat wants.
- **`--adopt`** is repeatable; see the ordering note above.
- **`--label`** carries the source agent's own creation label (`user_created` or
  `agent_created`), because that distinction drives the OOM shedding bands -- a
  recreated worker chat should still be shed before a user chat. An agent that had
  neither label defaults to `user_created=true` rather than being left unbanded.
- **No `--id`.** A fresh agent id is minted deliberately: the source's id may still
  be live on the old host, and two agents sharing an id is not a state mngr
  expects.

**Stopping each one immediately is the point**, not an optimization. The tab
renders from the adopted JSONL whether or not a process is running, so a stopped
agent shows its full history and costs nothing; recreating a dozen old chats
*running* would flood the workspace with live Claude processes and OOM pressure.
The user revives any of them by sending a message.

Every create is recorded in `data/.tasks/migrate-workspace/recreated-agents.jsonl`,
keyed by the **source** agent id, and a re-run skips what is already there. That is
what makes an interrupted pass -- or a deliberate second incremental pass -- safe to
repeat.

### Name collisions

An agent-name collision is cosmetic: an agent carries no wiring another agent
could conflict with. So `plan_agent_names` auto-suffixes (`<name>-2`, `<name>-3`)
and the rename is **reported**, not asked about. Apps, skills, and ports are the
opposite -- their wiring collides for real -- and the skill stops and asks for
those. The plan is keyed by source agent id, so two source agents that legitimately
share a name (a preserved agent whose name a later one reused) both get a distinct
name here rather than collapsing into one.

## 5. Why the primary agent is excluded

The source's `system-services` agent -- labelled `is_primary=true` -- is the one
agent never brought over. A workspace is *addressed by* its primary agent's id and
*discovered* via that `is_primary` label, so a second primary on this host makes
the workspace ambiguous to discovery. It is also not a chat: its window 0 is
`sleep infinity && claude`, and its real job is running bootstrap and the
background services, which this workspace's own primary already does. It usually
has no common transcript to migrate either.

`is_excluded_agent` matches it by label *and* by name, because a hand-made or
half-written `data.json` can be missing the label. Anything else -- chats, worktree
agents, `launch-task` workers, schedule agents -- comes over.
