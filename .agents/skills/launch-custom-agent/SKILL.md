---
name: launch-custom-agent
description: Launch a coding agent that works in the user's OWN repo or directory instead of this workspace's checkout. Use when a technical user wants an agent on their own codebase, with their own AGENTS.md, skills, and conventions -- the agent runs vanilla, free of this workspace's chat instructions, but still shows up as a chat in the Imbue Studio UI, bills through a signed-in provider account, and can notify the user.
metadata:
  author: imbue
---

# Launching a custom agent

A custom agent is a user-facing chat whose working directory is a repo or folder the
user chose -- not this workspace's checkout. Nothing project-scoped from this
workspace applies to it (no AGENTS.md, no tk, no workspace skills); the user's own
repo conventions govern it. It is created with the `custom` role template from
`.mngr/settings.toml`, which keeps the launch working outside the workspace checkout
and appends one context block telling the agent where it is running.

## 1. Gather what the launch needs -- all of it from the user

Two inputs are REQUIRED and must come from the user explicitly; never assume them:

- **Where**: an absolute path to their repo or directory inside this workspace. If
  they name a git URL instead, clone it first (under `/home/user/projects/<name>`,
  unless they say otherwise); authentication for private repos is theirs to provide
  -- ask how they want to authenticate rather than guessing.
- **Mode**: `worktree` (the agent gets its own git worktree of the repo, so parallel
  agents never collide; requires a git repo; uncommitted changes do not ride along)
  or `in-place` (the agent works directly in the directory, uncommitted changes and
  all). Present both and let the user pick; do not default.

Optional, worth offering: which provider account or harness to run on (default: the
workspace's default account), a `--branch` spec for worktree mode (e.g. `main` or
`main:feature-x`), a display name for the chat list, and a first task message.

## 2. Launch

Run from the workspace root (`/home/user/workspace`) -- the launcher resolves the
workspace's mngr config and account defaults from there:

```bash
uv run python .agents/skills/launch-custom-agent/scripts/launch_custom_agent.py \
    --name <short-kebab-slug> \
    --path /home/user/projects/their-repo \
    --mode worktree \
    --message "<the user's task, verbatim or lightly cleaned up>"
```

Other flags: `--account <id>`, `--harness <claude|codex|pi-coding|opencode|antigravity>`,
`--branch <spec>`, `--display-name <name>`, `--message-file <path>`. The script
validates the inputs, binds the agent to the account the way a chat is bound, runs
the `mngr create`, and prints the agent id and work dir. It refuses a taken name, a
missing path, and worktree mode on a non-git directory.

## 3. Report back

Tell the user the agent is running and where: its chat appears in the chat list
under the display name, and its working directory is the printed work dir (a
worktree lands at `/home/user/worktrees/<name>`). Destroying the agent later
(`mngr destroy <name>`) removes the agent but never deletes the user's source
directory.

The create may print "WARNING: No API credentials detected for Claude Code" --
that preflight only looks at the process env and `~/.claude`, while the agent's
credential rides its account binding. The warning is expected and harmless here.

## Guidelines

- The launched agent is a chat: it notifies the user itself when it finishes. Your
  own notification rules are unchanged -- this launch is usually not worth one.
- The custom agent loads the target repo's own AGENTS.md / CLAUDE.md / .claude
  config. That is the point: do not inject this workspace's conventions into it
  beyond what the `custom` template already appends.
- If the user wants several agents on the same repo at once, use worktree mode for
  each; two in-place agents in one directory will trample each other.
- The launcher relaxes mngr's settings-narrowing guard for its one create
  (`MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING=true`): the `custom` template
  deliberately replaces the per-harness workspace prompt blocks and codex's
  provisioning command, which that guard would otherwise reject.
- For claude, the launcher pre-trusts the agent's work dir in the account's
  config (the same seeding that trusts the workspace checkout); without it,
  claude's folder-trust dialog blocks the first message in a new directory.
