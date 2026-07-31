# Workspace internals

This workspace is created from default-workspace-template: a self-contained
template for running a persistent Claude agent that delegates work to
sub-agents and can manage its own apps and background services.

## Usage

```bash
mngr create my-workspace main -t local \
    --host-env MINDS_WORKSPACE_NAME=my-workspace \
    --project ~/project/default-workspace-template
```

## Creations

Users make "creations". There are conventions for the common kinds:

- an **app**: something the user opens as a tab and interacts with. Lives
  under `system/apps/<package>/`, runs as a supervisord program, registers its
  port via `forward_port.py`.
- a **skill**: teaches the mind how to do work the user cares about (including
  scripts and CLI tools, which ship inside the skill that knows how to use
  them). A skill that is automatically run on a schedule is called an
  "automation" -- the machinery that runs automations lives in
  `system/libs/automations/`, and the weekly Caretaker
  (`system/services/caretaker/`) is the built-in example. Lives under
  `.agents/skills/<name>/`.
- **data**: documents, images, notes, or data created by apps and skills.
  Lives under `data/`.
- **customizations**: changes to any of the above -- everything in the
  workspace can be modified.

A **service** is a background program with no tab -- usually supervised by
supervisord, sometimes cron-driven (the Caretaker). Standalone services live
in `system/services/`; a service that exists solely to support one app lives
in that app's folder and is named `<app>-<role>`.

## Structure

- `CLAUDE.md` - Agent instructions
- `apps` / `skills` - Top-level symlinks to `system/apps/` and
  `.agents/skills/` for discoverability
- `system/config/parent.toml` - Upstream repo for pulling updates
- `.mngr/settings.toml` - Agent types, create templates, command defaults
- `.agents/skills/` - Agent skills (task delegation, app building, self-update)
- `system/scripts/` - Utility and provisioning scripts
- `system/supervisord.conf` - Supervisord config defining the apps' and
  services' programs
- `system/apps/` - Everything tab-openable: `system_interface/` (the workspace
  web UI -- the special app that hosts the other tabs), `terminal/`,
  `browser/`, and every user-built app; registered in the uv workspace via the
  `system/apps/*` member glob
- `system/services/` - Standalone background services (`app_watcher/`,
  `caretaker/`, `cloudflare_tunnel/`, `eval_worker/`, `host_backup/`,
  `env_converge/`, `oom_priority/`)
- `system/libs/` - Support libraries, including `bootstrap/` (first-boot
  setup, then launches supervisord to supervise the apps and services) and
  `automations/` (the machinery that runs skills on a schedule)
- `data/` - Gitignored workspace data: documents and project folders, uploads,
  memories, tickets, secrets, machine state, and per-app data (see
  `data/README.md`)
- `system/vendor/mngr/` - A vendored, mutable copy of mngr. Note that making
  changes here *will* affect the behavior of the `mngr` command
- `system/vendor/tk/` - A vendored copy of the
  [tk](https://github.com/wedow/ticket) ticket tracker. The `ticket` script
  (also callable as `tk`) manages tickets stored as markdown. We point
  `TICKETS_DIR` at `data/.tickets/` (set in `.mngr/settings.toml`'s
  `host_env`) so tickets live alongside the rest of the workspace's data
  (covered by the restic host backup).

## Create templates

- `worker` - For sub-agents created via the launch-task skill (includes code review)
- `subskill-worker` - Sub-agent for any flow that hands its worker the generic harden worker (the crystallize / update / heal creation lifecycle, including the update-system-interface flow). Inherits from `worker` and pre-installs the single generic worker from `.agents/shared/worker/` into its own `.agents/skills/` as `harden-worker`.

## Creation harden lifecycle

The main agent can promote ad-hoc work into reusable creations, fix creations that fail, and extend creations that came up short -- across skills, apps, services, and the system interface. The user-invokable surface is three generic operation leads (main agent side), each parameterized by the creation type:

- `crystallize-creation` - Create a new creation (default: a skill reconstructed from the just-finished turn). Invoked directly post-turn, or by the live-half wrappers (`build-app`, `fetch-process-show`) once a prototype is confirmed.
- `heal-creation` - Fix a skill, app, or service that errored or produced wrong results.
- `update-creation` - Extend / refactor / verify a skill, app, service, or shared reference; one flow with a committed-vs-emergent design-gate toggle.

Each lead spawns a `subskill-worker` sub-agent that runs the single generic `harden-worker` sub-skill. The worker reads the operation and type from its task file and composes the universal `harden-creation.md` contract with one `op-*.md` and one `type-*.md` reference under `.agents/shared/worker/references/`. Workers commit to `mngr/<task-name>` branches; main merges on user approval. (The same template also backs the `update-system-interface` flow, which wraps `update-creation` with `type=system-interface` and adds its preview / safe-reveal go-live.)

Crystallized skills are marked with `metadata.crystallized: true` in their SKILL.md frontmatter and follow the [agentskills.io](https://agentskills.io/specification) layout (`scripts/run.py` as a PEP 723 script, companion SKILL.md, optional `references/` and `assets/`).
