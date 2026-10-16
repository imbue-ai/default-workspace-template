# Studio Discord

A window in Imbue Studio that gets people into the Imbue Studio Discord and lets them read it without leaving
Studio. Hardcoded to one server (Imbue) and its Studio channels; it never reads anything else on the user's
Discord account.

Status: working. Shows who is online, the join link, and (once the user connects their Discord) the latest messages in the three Studio channels. Every Discord reply is kept as received under the app's data folder.

- Plan: the workspace's `docs/system/specs/discord-app/plan.md`.
- What it needs from Studio, and how it gets it: [docs/studio-contract.md](docs/studio-contract.md). In short, the
  workspace registers the window and starts the app; everything else is HTTP. No workspace code is imported.

## Install into a workspace

Workspaces built from the default workspace template already carry it. For any other:

```
git clone https://github.com/boweiliu/studio-discord /home/user/workspace/.external_worktrees/studio-discord
python3 /home/user/workspace/.external_worktrees/studio-discord/install.py      # --port, --workspace, --uninstall
```

## Develop

```
uv sync
uv run pytest
uv run ruff check . && uv run pyright
supervisorctl restart studio-discord    # pick up edits in a workspace running this checkout
```
