---
name: install-app
description: Install one app someone published or shared (its own git repository, with an app.toml at its root) into this workspace, then set it up with the user -- permissions, secrets, and adaptation. Use when the user gives an app's repository URL or asks to add an app someone shared, or when a message is `/install-app <url>`. For a whole published workspace (a template), use use-template instead; for a newer version of an app already installed, update-installed-app.
metadata:
  author: imbue
---

# Installing an app

An app is one window on the desktop with everything it owns: its code, the
skills it carries, its routines. A published app lives in a git repository of
its own, with `app.toml` at the root. Installing it adds that one app to this
workspace and leaves everything else here as it is.

A template is different: it is a whole workspace. If the URL turns out to be a
template (step 3 says how you will know), hand over to the `use-template`
skill.

## Standing instructions

- **Run every command from the repo root** (`/home/user/workspace`).
- **Keep git and file words out of what you say to the user**: they are
  getting an app, not a repository
  (`.agents/shared/references/user-facing-language.md`).
- **Never run the installer before the user has agreed to trust the source**
  (step 2).
- **Never edit the app's program file or skill links.** They are generated
  from the app's `app.toml`. To change how it runs, change the manifest and
  run `uv run --no-sync app-install regenerate <name>`.
- **Done means the user can open the app and see their own data.** A program
  that starts or a route that answers 200 is not done.

## What the URL can be

- **A published app**: a public repository that holds one clean copy of the
  app per version. The latest version is installed unless you name one.
- **Someone's dev repository** (usually named `<name>-dev`): the private
  repository a person's own workspace keeps its working copy of an app in,
  which they have given the user access to. It installs the same way, from
  its `main` branch, since it has no versions. Two things differ, and the
  user should hear them: what arrives is the app as it stands in that
  person's workspace today, not a version they chose to release, and an
  update later takes whatever they have changed since. It is private, so the
  first result is usually `needs_github_access` (step 3), and the GitHub
  account the user approves must be one that person gave access to.

## Other tasks

- **Removing an app**: `uv run --no-sync app-install uninstall <name>`. The
  app's stored data and secret files are kept, and the result names them and
  what else is left; say so. The `update-app` skill has the whole teardown.
- **A newer version of an app already installed**: the `update-installed-app`
  skill.
- **Checking what is generated**: `uv run --no-sync app-install check` says
  whether everything generated for the workspace's apps is as it should be.

## Steps

1. Find which case this is.
2. Confirm the user trusts the source. *Ends the turn* until they say yes.
3. Install the app.
4. Set it up, before talking about changes. *Ends the turn* while a
   permission or a secret is awaited.
5. Adapt it with the user.

## Step 1: Find which case this is

Run `uv run --no-sync app-install list` and look for an app whose
`upstream_url` is the URL you were given.

- **The app is not listed.** Go on to step 2. This is also where a
  `/install-app <url>` message from the Imbue Studio app lands when its own
  install did not go through: it could not read the repository, or the
  install failed and this message is the second try.
- **The app is already listed.** The Imbue Studio app installs an app itself,
  before any chat exists, when the user creates a workspace from an app or
  adds one to a workspace they have, and then sends `/install-app <url>` so
  the setup continues here; or an earlier turn installed it. The user already
  agreed to trust it. Skip to step 4 and set it up. There is no installer
  result to work from in this case, so read what the app needs from its
  manifest at `system/apps/<package>/app.toml`: `[[permissions]]`,
  `[[secrets]]` (a file is missing when `data/.secrets/<file>.env` does not
  exist), `[llm]`, `[[dependencies]]` (missing when no app of that name is
  here), `[environment]`, and `[[adaptation]]`. Check each of its programs
  with `supervisorctl status`: one that runs under a secret file that does
  not exist yet is not running.

## Step 2: Confirm the user trusts the source

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

## Step 3: Install the app

```bash
uv run --no-sync app-install install <url>                  # the latest version
uv run --no-sync app-install install <url> --branch <ref>   # a particular version
```

The command asks nothing and ends in one JSON line. Act on its `result`:

- `installed` -- the app is in the workspace. `problems` lists anything that
  could not be done once it was (supervisord could not start its programs,
  say), and `is_answering` says whether the app's port answered; it is false
  while the app waits on a secret file, and otherwise means the app did not
  come up, so read its log (the `check-app-errors` skill) before you call it
  running. Go to step 4 with the `needs` it lists.
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
  (the `connect-external-service` skill has the command; scope `github-git`,
  permission `github-git-read`), end the turn, and run the installer again
  once it is granted.
- `unreachable` or `refused` -- say what went wrong in plain words, from
  `detail`.

## Step 4: Set it up, before talking about changes

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
   `uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart`
   starts the one named after the app, and `supervisorctl start <program>`
   each of the others (`<name>-<role>`).
   A `Secret declined:` message leaves the app unstarted; say so, and why.
3. **A model.** When `needs.llm` is present, the app calls a model. Follow the
   `use-ai-integration` skill to give it what its `method` needs.
4. **Other apps, and the data they collect.** `needs.missing_dependencies`
   names apps this one expects to find here, usually because they collect or
   process the data it shows. Nothing installs them for you, and getting the
   data to flow, so that every app here works, is yours to work out:
   - offer to install a missing one that has a `repo_url` under
     `[[dependencies]]`, the same way, and set it up before this app;
   - one with no `repo_url` is built in or was never published: the app's
     `[[adaptation]]` notes and its README say what it needs from it;
   - when an app already here collects the same data, have one of them
     collect it and the other ask that one (an ordinary change, the
     `update-app` skill) rather than run the same collection twice.

   Tell the user what you set up and what is still missing.
5. **System packages.** `needs.unresolved_environment` lists what could not be
   installed, with the reason. An apt package that does not exist at this
   workspace's pinned snapshot can be reached with
   `uv run --no-sync env-converge upgrade`; offer that. Do not press on into a
   setup that cannot work. The installer does not run the environment units
   the app carries (`system/apps/<package>/env.d/*.sh`): env-converge runs
   them at the next boot. When the app needs what one installs before then,
   run it by hand (`bash system/apps/<package>/env.d/<file>`, from the repo
   root).

Then open the app yourself (`verify_app.py <name> --open`), confirm it shows
the user's real content, tell them it is ready and invite them to look.

## Step 5: Adapt it with the user

`needs.adaptation` lists what the publisher knew would not carry over as it
is: each has a `summary` (what does not fit) and a `resolution` (what to ask
or change). Take them one at a time, in the user's terms. Changes you make
are ordinary changes to the app: use the `update-app` skill.

The app is yours to change. It stays connected to where it came from, so a
later version can be brought in on top of your changes: that is the
`update-installed-app` skill.

Once this workspace has changed the app and the change is saved, the app
counts as modified (`state` in `app-install list`). When GitHub sync is
enabled (`test -f data/system/github_sync.toml`), give it a private
repository of its own, which the sync service then keeps current, by running
`uv run --no-sync app-install link <name>` from the repo root of the live
workspace (the `github-sync` skill describes dev repositories). An app nobody
has changed has none, and `link` refuses it.
