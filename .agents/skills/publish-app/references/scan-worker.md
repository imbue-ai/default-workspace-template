# Getting an app ready to publish (the background worker's guide)

You are the worker `publish-app` dispatched. Your task file names the app
(`app`, `package`), the `version` agreed with the user, the `repository` it
will be published to, what to leave out, the skills the app takes in, and
what the app was derived from.
Do all of this in one run on your branch, with no back-and-forth unless you
are genuinely stuck.

Everything you write here is guidance for whoever installs the app, and it is
best effort: you are reading code, not proving anything. This pass is the only
time these fields are written, so read the app properly.

## Contents

- Before anything else
- 1. Leave out what the user asked to leave out
- 2. Take in the skills the app needs
- 3. Read the app and write what an adopter needs to know
- 4. Have the app depend on the released SDK
- 5. Settle the version and write it
- 6. Bring the README up to date
- 7. Check, commit, report

## Before anything else

From your worktree root:

```bash
eval "$(uv run --no-sync .agents/shared/scripts/agents_shared/parse_task_frontmatter.py data/.tasks/launch-task/<slug>/task.md)"
START=$(git rev-parse HEAD)
echo "TASK_FILE=$TASK_FILE START=$START"
```

Note both values: every command runs in a fresh shell.

Three limits:

- Change only files inside `system/apps/<package>/`, and what step 2 moves
  or repoints. The app's record (`system/config/apps/<name>.toml`) and its
  generated files are not yours to edit by hand.
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

## 2. Take in the skills the app needs

Skip this step when your task's "skills to take in" says `none`.

Whoever installs the app has nothing of this workspace, so a skill the app
needs is published inside it. Each skill your task names lives under
`.agents/skills/<skill>/` and moves into the app. An app's skill is named
after the app: `<carried>` below is `<name>-<skill>`, or `<skill>` itself when
it already starts with the app's name. For each one:

1. **Take it out of the record, move it, rename it.**

   ```bash
   uv run --no-sync app-install reference remove <name> .agents/skills/<skill>   # only when the record lists it
   mkdir -p system/apps/<package>/skills
   git mv .agents/skills/<skill> system/apps/<package>/skills/<carried>
   ```

   Set `name: <carried>` in its `SKILL.md` frontmatter, and bring what the
   skill says about itself (its heading, its own paths, its command) in line.
2. **Move its code into the app's package.** A skill an app carries has no
   `python/` project of its own. For a skill that has one:
   - move the modules and their tests from
     `skills/<carried>/python/<skill_with_underscores>_skill/` into the app's
     package, under `src/<package>/` in a module named for what it does, and
     delete `skills/<carried>/python/`;
   - add what its `pyproject.toml` depended on with `uv add`, run in the
     app's directory, and give each entry point a console script under
     `[project.scripts]` in the app's `pyproject.toml`;
   - have the skill's steps run
     `uv run --no-sync --project system/apps/<package> <script> ...`, and the
     app's own code import the modules directly;
   - take the skill's package out of the app's dependencies, and out of the
     root `pyproject.toml`'s `[tool.uv.sources]` when it is named there, then
     run `uv lock` at the repo root so the workspace's lock lets it go, and
     commit that lock.

   An app with no `pyproject.toml` cannot take in a skill that has a
   `python/` project: report `stuck` and say so.
3. **Keep its data with the app's.** Where the code reads or writes
   `data/.skills/<skill>/`, have it use a directory under the app's own data
   directory instead (`DATA_DIR`, which is `data/.apps/<name>/`), named for
   what it holds. Move no data yourself: it is not in your worktree. Name
   each pair of directories in your report, for the lead to move.
4. **Repoint everything else that named it.** Your task lists what else uses
   the skill. Make only the change that keeps each working:
   - a routine that runs the skill names `<carried>`
     (`skill = "<carried>"`). Look in the app's own `routines/` too: a
     routine names its skill without a path, and those are published with
     the app;
   - another app, or a skill that stays outside the app, that imported its
     package or ran its scripts calls this app instead: its console script,
     or its HTTP API, found by name (`resolve_app_url("<name>")`, never a
     port);
   - another app that now calls this one gets a `[[dependencies]]` entry for
     it in its own `app.toml`, and a skill that does is recorded with
     `uv run --no-sync app-install reference add <name> .agents/skills/<other> --note "<the surface it uses>"`;
   - another app whose references list the skill no longer lists it, since a
     reference never names a path inside an app:
     `uv run --no-sync app-install reference remove <other> .agents/skills/<skill>`,
     or, for a built-in app, delete the `[[references]]` entry in its
     `app.toml`. When the skill calls that app, step 3 lists it under this
     app's `[[dependencies]]`.
5. **Have the link written.**

   ```bash
   uv run --no-sync app-install regenerate <name>
   ```

   It writes the `.agents/skills/<carried>` link the skill is found by;
   commit it with the move. `collision` means another skill already has that
   name: pick another that starts with the app's name.

When you are done,
`git grep -n -e '.agents/skills/<skill>' -e 'data/.skills/<skill>' -e 'skill = "<skill>"'`
prints nothing (for a skill that kept its name, only the `data/.skills/`
lines have to be gone), and the app's tests pass with the modules where they
now are.

## 3. Read the app and write what an adopter needs to know

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
| `[[dependencies]]` | Other apps this one calls or gets its data from (`resolve_app_url("<other>")`, a link to another app's page, another app's data, what the Datalib app mirrors). | `name`, and `repo_url` when that app is published (its record's `published_repo_url`, or `upstream_url` for an installed one; a built-in app such as `datalib` has none) |
| `[[adaptation]]` | What will not fit someone else's workspace as it is: a hardcoded channel, account, folder or name, an assumption about data that only this user has, data another app or Datalib has to be set up to collect first (which sources, how far back). | `summary` (what does not carry over), `resolution` (what to ask, change or set up) |

Use the permission and scope names latchkey really has (the
`connect-external-service` skill lists them), not ones that sound right: an
adopter's agent files these as requests.

Anything hardcoded that is personal to the user (a name, an address, an
account id) is better removed than described. If it is quick and safe, make it
a setting the app reads and note it under `[[adaptation]]`; if it is not, note
it and say so in your report.

## 4. Have the app depend on the released SDK

Skip this step for an app with no `pyproject.toml`, and for one whose
`pyproject.toml` does not name `app-schema`.

An app built in a workspace gets the app SDK (`imbue.app_sdk`) from the
workspace: its `pyproject.toml` depends on `app-schema` through a path into
the workspace, which resolves nowhere else. A published app depends on the
released SDK, `imbue-app-sdk` on PyPI. In
`system/apps/<package>/pyproject.toml`:

- in `dependencies`, replace `"app-schema"` with
  `"imbue-app-sdk>=<version>,<1"`;
- in the `dev` dependency group, replace `"app-schema[testing]"` with
  `"imbue-app-sdk[testing]>=<version>,<1"`;
- delete the `app-schema` line under `[tool.uv.sources]` with the comment
  above it, and the table's heading when nothing is left under it;
- under `[tool.uv]`, add `exclude-newer-package = { imbue-app-sdk = false }`,
  so a fix to the SDK reaches the app without waiting out the two-week window
  the line above it sets.

`<version>` is the SDK the workspace runs, which is what the app was built
and tested on. From your worktree root:

```bash
uv run --no-sync python -c 'from importlib.metadata import version; print(version("imbue-app-sdk"))'
```

An app from before the SDK imports `app_schema.*` and
`workspace_layout.identity`. Change each import to the same name under
`imbue.app_sdk`: `app_schema.routes` is `imbue.app_sdk.routes`,
`app_schema.testing` is `imbue.app_sdk.app_testing`, and
`workspace_layout.identity` is `imbue.app_sdk.identity`. One name is not the
same: `app_schema.errors.AppSchemaError` is `imbue.app_sdk.errors.AppSdkError`.
Any other dependency that is a path into the workspace
(`path = "../../..."`) has to go as well; if the app cannot do without it,
report `stuck` and name it.

Then make the app's lock and environment again, and run its tests on them:

```bash
(cd system/apps/<package> && uv lock && uv sync && uv run --no-sync pytest -q)
```

When `uv lock` finds no `imbue-app-sdk` that satisfies the range, the
workspace runs an SDK newer than the newest released one:

```bash
curl -s https://pypi.org/pypi/imbue-app-sdk/json | python3 -c 'import json, sys; print(json.load(sys.stdin).get("info", {}).get("version", "none"))'
```

It prints the newest release, or `none` when no SDK has been released.
Lower `<version>` to the release that prints and run the three commands
again. If the app's tests pass there, go on. If they fail, or it printed
`none`, the app needs an SDK that is not out yet: report `stuck` and say so.
Never point the dependency at a path or a git URL to get past this.

## 5. Settle the version and write it

Your task carries the version agreed with the user. A **major** version tells
everyone who installed the app that they must act. If what you read in step 3
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
  steps 1 and 3: fix the manifest and run it again).

If the new version changes the shape of the app's stored data, the app must
carry its own migration: code that brings data written by an earlier version
forward when the new one first runs, with a test of it. Whoever installs the
update has only their own data and this app's code, and can skip versions, so
the app keeps a migration for every shape a published version ever wrote.

## 6. Bring the README up to date

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
5. **What it needs.** From the tables you wrote in step 3, in plain words:
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

## 7. Check, commit, report

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
- each skill the app took in: its new name, each pair of data directories the
  lead has to move (from under `data/.skills/`, to under
  `data/.apps/<name>/`), and what else you repointed;
- what whoever installs the app will be asked for, in one plain line per
  table you wrote;
- the `imbue-app-sdk` range the app now depends on, when step 4 changed it;
- anything you could not determine, or left hardcoded and noted instead of
  changing.

A `stuck` body quotes the failing command's output verbatim.

The lead may message you afterwards with a fix (a secret the scan found, a
name that changed, wording the user wants different). Make it on the same
branch, run this step's checks again, and report `done` again.
