---
name: add-repo-to-store
description: Work out how to install an app from a GitHub repository, test-run it safely, and hand the App Store a recipe for the person to confirm. Invoked as `/add-repo-to-store <github link>` when the App Store's Add app dialog is given a GitHub link, and as `/add-repo-to-store <github link> --update <app>` when an app added from GitHub is checked for a newer version; also use when the user asks to add or install an app from a GitHub link into the App Store, or to update one.
metadata:
  author: imbue
---

# Adding an app to the App Store from a GitHub repository

You are given a GitHub link. Your job ends with one of two things:

- **A recipe handed to the App Store**, which the person then confirms there (the store shows the exact commands,
  the cost, and that nobody reviewed it; nothing is installed until they click **Add to store**), or
- **One plain sentence saying why it can't run here**, with nothing handed over.

The recipe is an **install file** (format below). You never install the app for real, never call the store's
install or confirm routes, and never edit the store's data: the store checks your file by the same rules as its
built-in apps, and only the person's confirmation adds it. Repair and reinstall later follow the saved file, so
get it right once.

The store's side is in `system/apps/app_store/README.md` ("Adding an app from a GitHub repository").

## What the person sees

Use these as your progress titles (collapse or skip as the run goes): "Look up the project", "Work out how to install
it", "Try it out", "Hand it to the App Store". Talk about the app, not about recipes, commits or scripts: "it's a
Python app, trying it now", "it needs more memory to build than this workspace has". Give them the app's name and
what it does as soon as you know it.

## 1. Look up the project

```bash
python3 .agents/skills/add-repo-to-store/scripts/look_up_repo.py "<link>"
```

It pins the link to one full commit (a branch or tag link resolves to the commit it points at now) and prints, as
JSON: the repository, `commit`, `commit_date`, description, license, size, `is_archived`, top-level files, and the
text of the files that say how it installs and runs (`.imbue-store.toml`, `pyproject.toml`, `setup.py`,
`package.json`, `Dockerfile`, `docker-compose.yml`, `requirements.txt`, the README). It also keeps that raw look-up
in its `record_directory`; keep your install files there too.

- `"is_reachable": false` -- say its `what_happened` and stop. Only public repositories can be added for now.
- `is_archived` -- say so in your reply (it gets no fixes any more), and carry on.

**Which version.** A link to a branch, tag or commit means that one. A bare repository link (`asked_ref` is the
`default_branch`) means "this app": when the look-up shows a `latest_release`, use that release's `commit` --
the newest code on the default branch is often unreleased or a test version ("Glances 5 beta, do not use in
production"). Use the newest code only when there are no releases, or the README says the default branch is the
way to run it. Say which version you are adding in your reply ("the stable 4.5.8 release").

Everything below happens at one commit; never install a branch or tag name.

## 2. Work out how to install it

Read the README and the files it printed, then pick the **first** of these that fits:

1. **The repository ships `.imbue-store.toml`.** That is an install file its author wrote. Convert it to JSON, fill
   in `repository` and `commit` from the look-up if it leaves them out, and try it as is. Its figures are the
   author's claim; your trial measures the real ones.
2. **A Python package** (`pyproject.toml` or `setup.py` with a console script that serves a web page): recipe
   `{"kind": "python_git", "repository": ..., "commit": ..., "subdirectory": "", "python": "3.12"}`. The program is
   then `{env_dir}/bin/<script>`. Use `subdirectory` when the package is in a folder of the repository. Many apps
   keep their web server behind an optional extra (`pip install glances[web]`): ask for it with
   `"package_name": "glances", "extras": ["web"]`.
   When the version you picked is a release the project also publishes on PyPI (or npm, for item 3), the
   registry's copy of that release is fine too and usually installs faster: `{"kind": "python", "requirement":
   "glances[web]==4.5.8"}` (or `{"kind": "node", "package": "name@x.y.z"}`), with the file's `commit` set to that
   release's commit, so the confirmation links to the code it installs.
3. **A Node package with a `bin`** (`package.json`): `{"kind": "node_git", "repository": ..., "commit": ...}`. The
   program is `{env_dir}/node_modules/.bin/<bin name>`. A `start` script is a shell line and can't be used as is:
   work out the program and arguments it runs. A package whose install runs a big build (a `prepare` script
   running a bundler) may run out of memory; the trial will tell you.
4. **A site that runs entirely in the browser, published ready-made as a Docker Hub image** (an nginx or static
   image of the built site): `image_site`, exactly as the store's own Excalidraw entry
   (`system/apps/app_store/src/app_store/catalog/apps.toml`) does it -- find the image's linux/amd64 manifest with
   the registry API, pin the one layer whose files sit under the site folder, and serve it with a pinned
   `http-server`. Only for a static site: an image of a server program can't run here.
5. **Anything else that can be built into a static site**: `github_build` -- pinned npm tools, build steps run in
   the checkout, the built folder kept. `_recipe()` in
   `system/apps/app_store/src/app_store/github_build_test.py` is a complete example (Excalidraw v0.18.1). Builds are the hungriest route: prefer 4 when it exists.
6. **None of these**: it needs a separate database server, several services, Docker itself, a GPU, or a paid
   account to start; or it isn't something with a web page at all (a library, a command-line tool, a phone app).
   Stop and say so in one sentence: "Plausible needs a separate ClickHouse database, which this workspace can't
   run."

### The install file

```json
{
  "format": 1,
  "title": "Datasette Latest",
  "description": "One line, at most 200 characters, on what it is for.",
  "about": "A short paragraph a person who has never heard of it understands.",
  "license": "Apache-2.0",
  "repository": "https://github.com/simonw/datasette",
  "commit": "d1cc495a966cafaed3e50dad73cd88e790031704",
  "recipe": {"kind": "python_git", "repository": "https://github.com/simonw/datasette",
             "commit": "d1cc495a966cafaed3e50dad73cd88e790031704", "python": "3.12"},
  "launch": {"argv": ["{env_dir}/bin/datasette", "serve", "--host", "127.0.0.1", "--port", "{port}"]},
  "resources": {"disk_mb": 30, "memory_idle_mb": 80, "memory_in_use_mb": 90},
  "basis": "Tried at this commit on 2026-11-05: installed in 5 seconds, 27 MB, about 80 MB running."
}
```

The rules (the store refuses a file that breaks one, and says which):

- `title` becomes the app's name inside the workspace (lowercase letters and digits of it). If the store already
  offers an app of that name, the store refuses it: add a word ("Datasette Latest").
- `launch.argv` is a list, never a shell line. Its first item runs a program from the app's own environment
  (`{env_dir}/...`). It must pass `{port}` and listen on `127.0.0.1` only. Placeholders: `{port}`, `{data_dir}`
  (the app's own folder, kept across reinstalls: point its database or files there), `{env_dir}`.
- `launch.init_argv` runs once before the first start (e.g. `tiddlywiki {data_dir}/wiki --init server`).
  `launch.env` adds environment variables (placeholders allowed). `launch.setup_files` writes short settings files
  into `{data_dir}` before the first start: `[{"path": "config.yaml", "content": "port: {port}"}]`.
- A recipe that installs from the repository must name the same `repository` and `commit` as the file.
- An app is shown inside a window, which is a frame on another site. An app that refuses to be framed (an
  `X-Frame-Options` header, or a `frame-ancestors` policy) shows a blank window: find its own option that allows
  any site to frame it, and set it in `launch`.

## 3. Try it out

```bash
uv run --no-sync python -m app_store.try_install <install-file.json>
```

From the workspace root. It installs the file in a scratch folder with exactly the store's commands, runs its
setup, starts it, checks it answers and can be shown in a window (including a framed screenshot), measures disk
and memory, and always cleans up. A memory guard stops it before the workspace runs out. It prints one JSON result
and keeps the log, the screenshot and the result in its `record_directory`.

- `WORKS`: look at `screenshot_path` yourself (Read it). A blank page, an error page, or a login wall that a person
  cannot get past is not working, whatever the outcome says.
- `INSTALL_FAILED` / `START_FAILED`: read `output_tail` (and the log for more), fix the file, try again.
- `REFUSES_FRAMING`: find the app's option that allows framing (above), try again.
- `OUT_OF_MEMORY`: a lighter route, if there is one (a ready-made image instead of a build; a smaller variant); if
  not, stop and say it needs more memory than this workspace has.
- `INVALID_FILE`: fix what `what_happened` names.

Give up after **5** tries that didn't work, and say plainly what the last one ran into.

Before the first trial, `resources` can be a rough guess (`{"disk_mb": 50}`); the trial measures the real figures.
Only the app itself is pinned: the packages it depends on are resolved when it installs, as for any package the
store adds, so a later reinstall may pull newer ones.

### Asking the person

Ask only what you cannot decide yourself, and only once you need it: which of two ways to run it ("it can run as
a notebook or as a dashboard: which do you want?"), or whether to take a route with a real cost ("this version
builds from source and takes about 3 minutes to install: go ahead?"). An app that needs an API key or an account
to start is ending 6 for now: say what it needs. Never ask about anything the trial can find out.

## 4. Hand it to the App Store

Set `resources` from the working trial: `disk_mb` its `disk_mb` rounded up to the next 10, `memory_idle_mb` its
`run_memory_mb` rounded up to the next 10, `memory_in_use_mb` a quarter more than that, and for a `github_build`
also `build_disk_mb` and `build_memory_mb` from `install_peak_memory_mb`. Say how in `basis`, in one plain
sentence. Changing the words and figures needs no new trial; changing the recipe or the launch does (the hand-over
checks that its trial ran exactly the same recipe and launch).

```bash
uv run --no-sync python .agents/skills/add-repo-to-store/scripts/hand_over.py <install-file.json> --trial <the trial's record_directory>
```

`"is_handed_over": true` means the store is now holding it for the person. Put the store in front of them, where
the confirmation is waiting:

```bash
uv run --no-sync workspace-layout open app-store
```

Then tell them, briefly: what the app is, that it worked in a trial (its disk and memory), and that the App Store
is asking them to confirm. A refusal (`"is_handed_over": false`) says why; fix the file and hand it over again.

## Updating an app added from GitHub

`/add-repo-to-store <link> --update <app>` comes from the **Check for a newer version** button on the page of an app
already added from GitHub. Titles: "Look for a newer version", "Try it out", "Hand it to the App Store".

1. Read what it runs now: `curl -s http://127.0.0.1:8060/api/added-apps/<app>/install-file` (the store's address is
   the `app-store` row of `uv run --no-sync workspace-layout list`). It answers the current install file and the
   installed version.
2. Look the link up (step 1 above). The candidate is the `latest_release` commit when the current file was a
   release; when the current file ran the newest code of a branch (no releases, or the person chose it), the
   candidate is that branch's newest commit. Compare dates (`commit_date` against the current commit's, from
   `https://api.github.com/repos/<owner>/<name>/commits/<commit>`): only a **later** commit is an update. If there is
   none, say it is up to date ("Datasette Latest is on the newest version, from 1 November") and stop.
3. Start from the current file: same `title`, `description` and `launch`; the new `commit` in the file and in the
   recipe. Read the release notes or the commits in between for a changed start command, a new requirement, or a
   breaking change, and adjust. Try it out (step 3 above) exactly as for an add.
4. Hand it over with `--replaces <app>`:

   ```bash
   uv run --no-sync python .agents/skills/add-repo-to-store/scripts/hand_over.py <install-file.json> --trial <dir> --replaces <app>
   ```

   The store asks the person to confirm the update. Its files and settings are kept, it stops while the new version
   installs, and the version it has is put back if the new one fails to install or start. Then open the App Store as
   for an add, and tell them what changed in the new version in a sentence or two.
