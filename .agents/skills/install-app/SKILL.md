---
name: install-app
description: Install one app someone published (its own git repository, with an app.toml at its root) into this workspace, then set it up with the user -- permissions, secrets, and adaptation. Use when the user gives an app's repository URL or asks to add an app someone shared, or when a message is `/install-app <url>`. For a whole published workspace (a template), use use-template instead.
metadata:
  author: imbue
---

# Installing an app

An app is one window on the desktop with everything it owns: its code, the
skills it carries, its routines. A published app lives in a git repository of
its own, with `app.toml` at the root. Installing it adds that one app to this
workspace and leaves everything else here as it is.

A template is different: it is a whole workspace. If the URL turns out to be a
template (step 1 says how you will know), hand over to the `use-template`
skill.

All commands run from the repo root (`/home/user/workspace`). Keep git and
file words out of what you say to the user: they are getting an app, not a
repository (`.agents/shared/references/user-facing-language.md`).

## Which case this is

Run `uv run --no-sync app-install list` and look for the app.

- **The app is not listed.** Start at the trust gate (step 0).
- **The app is already listed** (the Imbue Studio app installed it while it
  created this workspace, or an earlier turn did). The user already agreed to
  trust it. Skip to step 2 and set it up; read what it needs from its manifest
  at `system/apps/<package>/app.toml`.

## 0. Trust gate -- confirm before installing

An app is code published by someone else, in a repository outside Imbue's
control. **Imbue does not review, verify, or vouch for apps.** An installed
app runs with everything this workspace can do -- its files, its connected
accounts, its network -- and could contain mistakes or malicious code. You
cannot rule that out by reading it, so the safeguard is the user's informed
consent.

BEFORE running the installer, tell the user in plain language that you are
about to add code that **Imbue has not verified and that could be malicious**,
name where it comes from, and ask them to confirm they trust that source and
want to go ahead. Do not install until they say yes. If they decline, stop.

## 1. Install

```bash
uv run --no-sync app-install install <url>                  # the latest version
uv run --no-sync app-install install <url> --branch <ref>   # a particular version
```

The command asks nothing and ends in one JSON line. Act on its `result`:

- `installed` -- the app is in the workspace and its programs were started.
  Go to step 2 with the `needs` it lists.
- `collision` -- something here already has a name the app would take;
  `problems` names each one. Nothing was changed. Tell the user what clashes
  and ask whether to remove the thing already here or leave the app out. There
  is no way to install it under another name.
- `not_an_app` -- the repository has no app at its root. If it holds a
  `template.md` or a `.mngr/settings.toml`, it is a template: follow the
  `use-template` skill with the same URL. Otherwise tell the user this link is
  not an app you can install.
- `needs_github_access` -- the repository is private to someone. Request the
  GitHub read permission yourself, as any other latchkey permission request
  (`.agents/skills/connect-external-service/references/latchkey.md`, scope
  `github-git`, permission `github-git-read`), end the turn, and run the
  installer again once it is granted.
- `unreachable` or `refused` -- say what went wrong in plain words, from
  `detail`.

## 2. Set it up, before talking about changes

Work from `needs` in the installer's result (or from the manifest, when the
app was already installed).

1. **Permissions.** For each entry of `needs.permissions`, file one latchkey
   permission request yourself, each in a tool call of its own, one after
   another without waiting for verdicts. Do not just tell the user a permission
   is needed: send the request so it appears for them to approve. These are the
   publisher's best guess; if the app turns out to need another, request that
   one too.
2. **Secrets.** For each entry of `needs.secrets` whose `is_present` is false,
   run the `connect-external-service` skill's `request_secret.py` with the
   entry's `file` and `variables`, and its `note` in your rationale, each in a
   tool call of its own, then end the turn. When a `Secret stored:` message
   arrives, start the app's programs (the `programs` the installer listed):
   `uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart`.
   A `Secret declined:` message leaves the app unstarted; say so, and why.
3. **A model.** When `needs.llm` is present, the app calls a model. Follow the
   `use-ai-integration` skill to give it what its `method` needs.
4. **Other apps.** `needs.missing_dependencies` names apps this one expects to
   find here. Nothing installs them for you. Tell the user which are missing;
   when the app's manifest gives one a `repo_url` under `[[dependencies]]`,
   offer to install it the same way.
5. **System packages.** `needs.unresolved_environment` lists what could not be
   installed, with the reason. An apt package that does not exist at this
   workspace's pinned snapshot can be reached with
   `uv run --no-sync env-converge upgrade`; offer that. Do not press on into a
   setup that cannot work.

**Done means the user can open the app and see their own data.** A program
that starts or a route that answers 200 is not done. Open the app yourself
(`verify_app.py <name> --open`), confirm it shows the user's real content, then
tell them it is ready and invite them to look.

## 3. Adapt it with the user

`needs.adaptation` lists what the publisher knew would not carry over as it
is: each has a `summary` (what does not fit) and a `resolution` (what to ask
or change). Take them one at a time, in the user's terms. Changes you make
are ordinary changes to the app: use the `update-app` skill.

The app is yours to change. It stays connected to where it came from, so a
later version can be brought in on top of your changes.

## Things to know

- **Never edit the app's program file or skill links.** They are generated
  from the app's `app.toml`. To change how it runs, change the manifest and
  run `uv run --no-sync app-install regenerate <name>`.
- **Removing it** is `uv run --no-sync app-install uninstall <name>`. The
  app's stored data and secret files are kept; say so.
- **`uv run --no-sync app-install check`** says whether everything generated
  for the workspace's apps is as it should be.
