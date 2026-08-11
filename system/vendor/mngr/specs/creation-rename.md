# The creation rename

Status: implemented (July 2026) on paired branches `mngr/creation-rename` in
this repo and in default-workspace-template. This spec records the taxonomy,
the decisions behind it, and the change inventory across both repos, so later
work (the scheduled-agent PR, the discovery PR, minds UI copy) can build on a
single vocabulary.

Audience: developers of minds and default-workspace-template (shorthand: dwt).

## The taxonomy

Users of minds make **creations**. "Creation" is used only at the highest
conceptual level (the workspace README's blurb, the glossary); everywhere else
the working vocabulary is the kinds:

1. an **app**: something the user opens as a tab and interacts with. Never
   "application", never "web service" -- always "app".
2. a **skill**: teaches the mind how to do work the user cares about. Scripts
   and CLI tools live inside the skill that knows how to use them. A skill
   that is automatically run on a schedule is called an **automation** (the
   scheduling primitive lands in a separate PR).
3. **data**: documents, images, notes, or data created by apps and skills.
4. **customizations**: changes to any of the above. Not a standalone kind --
   everything in minds can be modified by the user.

A **service** is exclusively a *background* supervisord program with no tab
(host-backup, cloudflared, the app watcher). An **inspiration** is a
publishable, bootable snapshot of a mind's creations (zero or more, plus
customizations); the template state a mind started from is its **template
base** (formerly "creation snapshot").

Retired terms: "artifact" (the old lifecycle machinery word), "web service",
"application(s)", "program" in prose (supervisord's `[program:*]` syntax is
untouched), and the pre-declutter "creations/" folder. Repo-wide ratchets in
dwt's `system/test_meta_ratchets.py` keep these out of live agent-facing
prose.

### Apps with background components

The rule, in preference order (documented in dwt's `build-app` skill):

1. Default: the app does its work on request -- no background component.
2. Recurring refresh: an automation (a scheduled skill) that writes into the
   app's data dir (once scheduling lands).
3. Truly continuous needs: a co-owned service -- its code lives inside the
   app's folder, its supervisord program is named `<app>-<role>`, and it dies
   with the app. A service lives standalone in `system/services/` only when it
   serves more than one consumer.

## The dwt tree

```
apps -> system/apps            # discoverability symlink
skills -> .agents/skills       # discoverability symlink
data/
  documents/  my-project/      # visible starters; users reshape freely
  uploads/                     # attachment inbox (recreated on demand)
  .apps/<name>/  .skills/<name>/   # machinery-managed per-creation state
system/
  apps/       # tab-openable: system_interface (the special app that hosts
              # the tabs), terminal, browser, and every user-built app
  services/   # tab-less daemons: app_watcher, cloudflare_tunnel,
              # host_backup, env_converge, oom_priority
  libs/       # support libraries: bootstrap, github_sync,
              # mngr_cli_contract, tk_command_parsing
```

The convention for `data/`: everything visible is the user's to organize;
dot-prefixed folders are machinery. `data/chat-files/` and `data/chat-images/`
were removed -- shared files live in sensible visible homes (a project folder,
`data/documents/`, `data/images/`) and the system interface serves them from
wherever they are (its file serving was already path-agnostic).

The uv workspace members are the three `system/{libs,services,apps}/*` globs
(`system/apps/terminal` is excluded: it is a shell wrapper around the vendored
`mngr_ttyd` plugin, not a Python package). The changelog gate, meta ratchets,
and update-self path classifier all understand the three-way split.

## Renamed machinery

- `data/.state/applications.toml` -> `data/.state/apps.toml`, with `[[apps]]`
  entries, the `MINDS_APPS_FILE` override, and the `.apps.lock` lock file.
  Identifier sweep on both sides of the system_interface WS protocol
  (`apps_updated` message, `AppEntry`).
- Skills: `build-web-service` -> `build-app`, `update-service` -> `update-app`
  (covers apps and their co-owned services; standalone daemons enter as
  `type: service`).
- Lifecycle leads: `crystallize/update/heal-artifact` ->
  `crystallize/update/heal-creation`, parameterized by
  `type: skill | app | service | system-interface` (the task-file frontmatter
  key changed from `artifact` to `type`; the parser is schema-agnostic, so
  only prose changed). Worker references: `harden-artifact.md` ->
  `harden-creation.md`, `artifact-*.md` -> `type-*.md`, plus a new thin
  `type-service.md`. The gate name `final-artifact` -> `final-creation`.

## Deliberately unchanged (plumbing)

- The `/service/<name>/` URL segment, `service:` layout refs,
  `events/services/events.jsonl`, and the Cloudflare hostname scheme -- an
  in-flight discovery PR supersedes them.
- supervisord `[program:*]` syntax.
- launch-task's `source_artifacts_dir` frontmatter key (the file-staging
  sense of "artifacts", unrelated to the retired lifecycle term).
- The mind/agent/workspace vocabulary, the system-interface/UI naming (a UI
  refactor is in flight), and minds UI strings like the servers page.
- Historical records: per-PR changelog entries, `docs/system/blueprint/`
  plans, and archived specs keep their original vocabulary.

## mngr-repo (minds) changes

- `apps/minds/docs/workspace/glossary.md`: creation, app, service, automation
  (marked future), customization, inspiration, and template base entries;
  "application" retired.
- Minds workspace docs, README, and overview: `apps.toml`, the three-way
  `system/` split, app vocabulary.
- `backup_workspace_scripts.py`: `system/services/host_backup` added as the
  first backup-code path candidate (the resolver already probed per-layout
  candidates: `system/libs/host_backup` for the declutter layout,
  `libs/host_backup` pre-declutter), with test coverage for the new layout.
- `test_snapshot_resume.py` reads the app registry at `apps.toml` with a
  fallback to `applications.toml` so it spans template versions.

## Compatibility notes

- Nothing had shipped: the declutter tree never reached dwt's released tags,
  so no live minds or published inspirations reference the intermediate
  layout, and no `update-self` migration step is needed for it.
- The backup-update contract (`[program:host-backup]`, the `host-backup`
  pyproject registration, `uv run host-backup`) is unchanged; only the
  package's directory moved, and the minds-side resolver handles all three
  historical layouts.
- `.dockerignore` remains a symlink to `.gitignore` (enforced by a meta
  ratchet); the data-layout ignore ladder was rewritten for the new folders.

## Open follow-ups

- The automation definition activates when the scheduled-agent PR lands
  (docs currently mark it as coming).
- The discovery PR may retire the `/service/` URL segment and the
  `events/services/` path; nothing in this rename depends on them.
- Minds UI copy (servers page, create flow) adopts the vocabulary with the
  in-flight UI refactor.
