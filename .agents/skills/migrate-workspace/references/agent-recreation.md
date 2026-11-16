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

`migrate_workspace.py list-agents --host-dir <host_dir> --accounts-dir <home>/.minds/accounts`
does this in one batched remote read. What it reads, and why:

- **`<host_dir>/agents/<agent_id>/data.json`** -- every agent still present on the
  source, running *or* stopped-but-not-destroyed. Note the directory is keyed by
  **agent id**, not name; `data.json` carries the `name`, `id`, `labels`, the
  harness (`type`) and the folder the agent worked in (`work_dir`).
- **`<host_dir>/preserved/<agent_name>--<agent_id>/`** -- agents that were
  destroyed. mngr preserves their session transcripts and history file on destroy
  precisely so they stay recoverable, so a destroyed chat the user still cares
  about is migratable.
- **`<host_dir>/{agents,preserved}/*/claude_session_id_history`** -- the
  append-only `"<session_id> <source>"` log a SessionStart hook writes, oldest
  first, for a claude agent.
- **`<host_dir>/{agents,preserved}/*/codex_root_session`** -- the id of the
  rollout a codex agent resumes, which mngr records for it.

`<host_dir>` is `/home/user/.mngr` on a current-layout source and `/mngr` on a
pre-declutter one; `detect-layout` reports it.

Each agent comes back with its `harness`, the `account_id` the chat app bound it
to (its `account` label; empty for an agent on no account), and its `work_dir`.
An agent whose `data.json` is missing or unparseable comes back in `unreadable`
rather than being skipped silently -- a half-written state dir is worth mentioning,
not worth guessing about. An agent with **no** session file has no transcript to
adopt; recreating it produces an empty tab, so ask the user whether they want it
at all. Sessions are enumerated for claude and codex agents only; an agent on
another harness is listed with none.

## 2. Why the history file is load-bearing

An Imbue Studio chat agent's `CLAUDE_CONFIG_DIR` is the provider account it was created
on (`~/.minds/accounts/<id>/`), and every chat on the same account shares one
`projects/` tree with nothing but the session id to tell them apart. There is no
per-agent directory to read. `claude_session_id_history` is therefore the only
thing that says which sessions belonged to which agent.

Two consequences for a migration. Sessions are spread across as many `projects/`
trees as the user has accounts, so a sweep has to walk all of them -- and a chat
whose account was deleted has no tree at all, because deleting an account removes
its credentials (its transcripts are kept, but nothing points at them any more).

That tree is under the account's folder on a current source, and under the
*primary* agent's state dir on one from before accounts had folders of their own:

```
<home>/.minds/accounts/<account_id>/projects/<encoded-work-dir>/<session_id>.jsonl
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

### Codex rollouts

Codex keeps each agent's transcripts in the agent's own codex home, date-nested:

```
<host_dir>/agents/<agent_id>/plugin/codex/home/sessions/YYYY/MM/DD/rollout-<timestamp>-<session_id>.jsonl
```

There is no shared tree to disambiguate, so a codex agent needs no history file:
`resolve_codex_session` picks the rollout named by the agent's
`codex_root_session` (the one mngr resumes), or the newest rollout when no root
is recorded, and reports a recorded root with no file the way an unresolved
claude id is reported. One rollout is adopted per codex agent.

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

## Accounts

The chat app keeps one folder per signed-in provider account under
`<home>/.minds/accounts/`, plus an `index.json` naming them and the default for
new chats. A chat's sign-in lives in its account's folder, and so do a claude
chat's transcripts, so a recreated chat needs the same folder here; the
binding below points at it. When this workspace has no accounts yet (no
`index.json`, or one listing none), bring the whole directory over as it is,
index included, so the old workspace's default account carries across:

```bash
rsync -a --ignore-existing -e "ssh -i /tmp/mind_key -p <port> -o StrictHostKeyChecking=no" \
    "<user>@<host>:<home>/.minds/accounts/" "$HOME/.minds/accounts/"
```

When accounts already exist here (a sign-in made while setting up the
migration, say), the same command brings over only the folders this workspace
lacks and leaves the index alone, since the index is the app's to write: tell
the user which accounts arrived and which was the old workspace's default, and
they register and pick a default from the app's own account list.

## 3. Stage the session files locally

`--adopt` resolves a **local** path, so the session files have to be here first.
Copy the whole set in one pass, keeping the source's tree, then hand the
directory to `recreate-agents`:

```bash
mkdir -p data/.tasks/migrate-workspace/sessions
rsync -a -e "ssh -i /tmp/mind_key -p <port> -o StrictHostKeyChecking=no" \
    --include='*/' --include='*.jsonl' --exclude='*' --prune-empty-dirs \
    "<user>@<host>:<host_dir>/agents/" data/.tasks/migrate-workspace/sessions/agents/
rsync -a -e "ssh -i /tmp/mind_key -p <port> -o StrictHostKeyChecking=no" \
    --include='*/' --include='*.jsonl' --exclude='*' --prune-empty-dirs \
    "<user>@<host>:<home>/.minds/accounts/" data/.tasks/migrate-workspace/sessions/accounts/
```

The tree is kept deliberately: `recreate-agents` finds each remote path's
basename anywhere under the staging dir (session ids are UUIDs, so basenames
cannot collide), and codex's adopt copies the `sessions/` tree above a rollout,
which a flattened copy would not have. Repeat the first command for
`<host_dir>/preserved/` if any destroyed agent is being brought over; a re-run
re-syncs what is already there without harm.

## 4. Recreate, dormant

```bash
uv run --no-sync .agents/skills/migrate-workspace/python/migrate_workspace_skill/migrate_workspace.py recreate-agents \
    --agents-json data/.tasks/migrate-workspace/agents.json \
    --sessions-dir data/.tasks/migrate-workspace/sessions \
    --source-repo-root <repo_root>
```

Per agent it runs, then immediately `mngr stop <name>`:

```bash
mngr create <name> --type <harness> --template chat --no-connect \
    [--from :<work_dir>] \
    --adopt <session-1.jsonl> [--adopt <session-2.jsonl> ...] \
    --label user_created=true [--label project=<project>] \
    [<account binding> --label account=<account_id>]
```

Each flag is doing a specific job:

- **`--type <harness>`** selects the harness the way the chat app does: the
  workspace defines no per-harness create template, so `--template claude` is
  not a thing. A claude agent comes back as claude, a codex agent as codex.
- **`--template chat`** is how the chat app creates a chat agent, so the
  recreated agent gets a tab and the user-facing output style. The template
  also runs it in place (`transfer = none`) rather than cutting a worktree,
  which would isolate it from the migrated tree.
- **`--from :<work_dir>`** appears only for an agent that worked outside the
  checkout -- a project-folder agent, working in one of the user's own clones
  or worktrees. It is recreated in that folder, which the data step has to have
  brought over at the same path first (`recreate-agents` skips it and says so
  otherwise), and stays the project-folder agent it was rather than becoming a
  chat in the workspace checkout.
- **`--adopt`** is repeatable; see the ordering note above.
- **`--label`** carries the source agent's own creation label (`user_created` or
  `agent_created`), because that distinction drives the OOM shedding bands -- a
  recreated worker chat should still be shed before a user chat. An agent that had
  neither label defaults to `user_created=true` rather than being left unbanded.
- **The account binding** is the one the chat app gives a chat it creates
  (`system/apps/chat/imbue/chat/harnesses/account_binding.py`), and it has to
  ride the create, since mngr starts the agent before returning. Claude is bound
  through `--env CLAUDE_CONFIG_DIR=<accounts>/<account_id>`, the config dir
  mngr sources from the agent's env file. The adopted sessions are not re-filed
  there by that flag alone: this workspace runs claude without a per-agent
  config dir, and in that mode mngr files an adopted session under the
  `CLAUDE_CONFIG_DIR` of the process running the create -- yours, which names
  your own account's folder -- so `recreate-agents` runs each claude create
  with that variable set to the chat's account folder, and the sessions land
  where the recreated chat reads them. Codex is bound by an
  `--extra-provision-command` that repoints the `auth.json` link under the
  agent's own codex home at the account's copy, after provisioning and before
  the start. `--label
  account=<account_id>` is what the chat app reads to show which account the
  chat is on. An agent with no account label gets no binding and runs on this
  workspace's default account; the account folder has to be here first (the
  skill's accounts step), or the agent is skipped with the reason.
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
