# Getting an app ready to publish (the background worker's guide)

You are the worker `publish-app` dispatched. Your task file names the app
(`app`, `package`), the `version` agreed with the user, the `repository` it
will be published to, what to leave out, and what the app was derived from.
Do all of this in one run on your branch, with no back-and-forth unless you
are genuinely stuck.

Everything you write here is guidance for whoever installs the app, and it is
best effort: you are reading code, not proving anything. This pass is the only
time these fields are written, so read the app properly.

## Contents

- Before anything else
- 1. Leave out what the user asked to leave out
- 2. Read the app and write what an adopter needs to know
- 3. Settle the version and write it
- 4. Bring the README up to date
- 5. Check, commit, report

## Before anything else

From your worktree root:

```bash
eval "$(uv run --no-sync .agents/shared/scripts/agents_shared/parse_task_frontmatter.py data/.tasks/launch-task/<slug>/task.md)"
START=$(git rev-parse HEAD)
echo "TASK_FILE=$TASK_FILE START=$START"
```

Note both values: every command runs in a fresh shell.

Three limits:

- Change only files inside `system/apps/<package>/`. The app's record
  (`system/config/apps/<name>.toml`) and its generated program file are not
  yours to edit.
- Never run `app-install publish assemble`, `push` or `record`: those are the
  lead's, after the user has looked at what you prepared.
- Never write a secret's value anywhere. A manifest names the variables a
  secret file sets, never what they are set to.

## 1. Leave out what the user asked to leave out

For each glob your task lists under "leave out of what is published", add it
to the manifest's `[publish] exclude` (gitignore syntax, relative to the app's
directory). Keep what is already there.

```toml
[publish]
exclude = ["docs/internal/**"]
```

`app.toml` and the icon it names can never be left out: without them the
repository is not an app.

## 2. Read the app and write what an adopter needs to know

Read the app's code, its skills, its routines and its environment units, and
write or refresh these tables in `system/apps/<package>/app.toml`. Remove an
entry that no longer applies; an app that needs none of a kind has no table of
that kind.

| Table | What to look for | Fields |
| --- | --- | --- |
| `[[permissions]]` | Every call that goes through `latchkey` (`latchkey curl`, a gateway URL, an MCP server reached through it): which service, and whether it reads or writes. | `scope` (the latchkey scope, such as `slack-api`), `permission` (such as `slack-read-all`), `note` (one line: what the app uses it for) |
| `[[secrets]]` | Every `data/.secrets/<file>.env` the app or one of its services runs under, and every variable it reads from one. The table already exists when the app's program runs under a secret file; check the variables are all listed. | `file`, `variables`, `note` (what the value is and where to get it) |
| `[llm]` | Whether the app calls a model. | `method` (`keyed` for an API key through the workspace's proxy, `keyless` for a subscription-backed CLI), `note` (what it uses the model for) |
| `[environment]` | System packages and global tools the app shells out to or imports from outside its own project. Not its Python dependencies (its `pyproject.toml` has those) and not its environment units (the files under `env.d/` are the list). | `apt` (names), `npm_global`, `uv_tools`, `cargo_crates` (name to version), `cargo_default_toolchain` |
| `[[dependencies]]` | Other apps this one calls (`resolve_app_url("<other>")`, a link to another app's page, another app's data). | `name`, and `repo_url` when that app is published (its record's `published_repo_url`, or `upstream_url` for an installed one) |
| `[[adaptation]]` | What will not fit someone else's workspace as it is: a hardcoded channel, account, folder or name, an assumption about data that only this user has. | `summary` (what does not carry over), `resolution` (what to ask or change) |

Use the permission and scope names latchkey really has (the
`connect-external-service` skill lists them), not ones that sound right: an
adopter's agent files these as requests.

Anything hardcoded that is personal to the user (a name, an address, an
account id) is better removed than described. If it is quick and safe, make it
a setting the app reads and note it under `[[adaptation]]`; if it is not, note
it and say so in your report.

## 3. Settle the version and write it

Your task carries the version agreed with the user. A **major** version tells
everyone who installed the app that they must act. If what you read in step 2
means they must (the shape of the app's stored data changed since the last
published version, or it needs a secret or a permission it did not need
before) and the agreed version is not a major one, use the next major version
instead and say so in your report: the lead shows the user the final version
before anything is published.

```bash
uv run --no-sync app-install publish set-version <name> <version>
```

It writes the version into `app.toml`, with `verified_template` set to the
template release this workspace is on, and for a Python app into
`pyproject.toml`, whose lock it makes again. Act on `result`:

- `versioned`: `changed_paths` are yours to commit. An entry in `problems`
  about the lock means `uv.lock` still names the old version: run
  `uv lock --project system/apps/<package>` yourself and fix what it reports.
- `refused`: `detail` says why (the version is not newer than the last
  published one, or the manifest does not pass its check after your edits in
  steps 1 and 2: fix the manifest and run it again).

If the new version changes the shape of the app's stored data, the app must
carry its own migration: code that brings data written by the earlier version
forward when the new one first runs. Whoever installs the update has only
their own data and this app's code.

## 4. Bring the README up to date

`system/apps/<package>/README.md` is the repository's landing page: the first
thing a person sees, and what convinces them to install the app. Write it for
a person, in this order:

1. **The app's name and one sentence on what it is.**
2. **The "Open in Imbue Studio" button**, exactly this, with the repository
   from your task in both places (`<owner>/<repo>` is the path of the task's
   `repository`):

   ```html
   <p align="center">
     <a href="https://studio.imbue.com/open?kind=app&amp;git_url=https://github.com/<owner>/<repo>"><img alt="Open in Imbue Studio" height="64" src="https://img.shields.io/badge/Open%20in%20Imbue%20Studio-D8D1C0?style=for-the-badge"></a>
   </p>

   Or paste this into a workspace's chat:

       /install-app https://github.com/<owner>/<repo>
   ```

   The button goes to the Studio page that opens the app, never to a bare
   `imbue-studio://` link, which GitHub renders dead. `kind=app` is what makes
   that page install an app instead of creating a workspace from a template.
3. **Why you would want it.** One or two plain sentences on the problem it
   solves, not how it is built.
4. **How to use it.** What someone does with it once it is running: its
   screens, what it does on a schedule, the skills it adds. Short, with a
   worked example where one helps.
5. **What it needs.** From the tables you wrote in step 2, in plain words:
   the accounts it connects to, the keys it asks for and where to get them,
   whether it uses an AI model.
6. **Ideas for making it yours.** Three to five concrete changes someone
   could make after installing it.
7. **Where it came from**, only when your task's "derived from" names a
   repository: one line saying this is a version of the app published there,
   with the link.
8. **License.** One line: MIT.

Mock data only: no real names, addresses, messages or screenshots of the
user's own data. Do not show the README to the user; the lead does that.

## 5. Check, commit, report

Commit your work on your branch, then run all three, fixing what they report
and committing again until they are clean:

```bash
uv run --no-sync app-manifest validate-manifest system/apps/<package>/app.toml
uv run --no-sync app-install check
uv run --no-sync app-manifest select-tests --diff-base "$START"
```

The first is the strict check a published manifest must pass. The last prints
the test gate your change calls for: run what it prints, in order. `git
status` must be clean when you finish.

Then report, per `.agents/shared/references/worker-reporting.md`:

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py report \
    --task-file "$TASK_FILE" --type status --name done --body-file <BODY_FILE>
```

Valid `name:` values: `question` (a gate, at any point), `done`, `stuck`. A
`done` body gives:

- the branch (`mngr/<slug>`) and its commit (`git rev-parse HEAD`);
- the version you wrote, and why if it is not the one in your task;
- what whoever installs the app will be asked for, in one plain line per
  table you wrote;
- anything you could not determine, or left hardcoded and noted instead of
  changing.

A `stuck` body quotes the failing command's output verbatim.

The lead may message you afterwards with a fix (a secret the scan found, a
name that changed, wording the user wants different). Make it on the same
branch, run this step's checks again, and report `done` again.
