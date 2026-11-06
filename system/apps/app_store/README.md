# App Store

The App Store app: one-click installs of a curated catalog of open-source web apps, the apps this workspace
already has, and the published template catalog. Served on port 8060 by the `app-store` package
(`app-store` program, `system/supervisord.conf.d/app-store.conf`).

## What it shows

- **Apps.** First the apps this workspace has -- every openable row of the app registry
  (`data/.state/apps.toml`), with the icon it registered, labelled built in, made in this workspace, or from
  the App Store -- each with Open (through the shell's `openLink`); a store app's tile also says what its
  processes use right now (or that it is not running), and its page has Uninstall. Then the catalog's apps
  still to install, each with its estimated disk and memory against what the workspace has free right now, and
  a Fits / Tight / Won't fit verdict. Memory is estimated twice -- idle, and while one person uses the app the
  ordinary way -- and the verdict checks the in-use figure. An app that won't fit cannot be installed, and the
  page says why. Filters above the lists ("All apps", then the catalog's categories) narrow both lists; the
  built-in apps carry no categories, so they show under "All apps" only.
- **Templates.** Getting Started's shelves of the published catalog (`catalog/README.md`). A template's page
  shows what it needs, its download size (its GitHub repository's size, cached), and that its memory is not
  known. Install sends `/use-template <repository>` through the shell's `startWithText`, so the adopt flow's
  trust check and guided merge (`.agents/skills/use-template/`) apply unchanged.

## The catalog

`src/app_store/catalog/apps.toml`, with one icon per app in `catalog/icons/` (drawn to
`docs/system/app-icons.md`). It ships in the package and is never fetched: an install recipe is code that runs
in the user's workspace, so it is reviewed and versioned with the workspace. `catalog.py` validates it when the
store starts:

- a recipe is a discriminated union on `kind`: `python` (a pinned `name==version` requirement installed by
  `uv` into the app's own venv) or `node` (a pinned `name@x.y.z` package installed by `npm --prefix`). Add a
  kind by adding a model to the union and its commands to `system.install_commands`.
- a third kind, `github_build`, builds a reviewed app from one exact commit of its GitHub repository: pinned npm
  tools (the build tool and the server) go into the app's environment, the commit is fetched by its hash, the
  build steps (argv lists, only `{env_dir}` substituted) run in the checkout, and only the built site is kept, as
  `{env_dir}/site`. Its `resources` also carry `build_disk_mb` and `build_memory_mb`, and the fit check uses
  whichever is larger, building or running (Excalidraw's build needed about 3 GB of memory for under a minute).
- a fourth kind, `image_site`, installs a reviewed app its own project publishes ready-made as a Docker Hub image:
  pinned npm tools (the server) go into the app's environment, then `python -m app_store.fetch_image_layer` downloads
  the one image layer holding the built site by its digest (anonymous pull token; the sha256 is checked, so a changed
  image is refused), and only the named folder inside it is unpacked, as `{env_dir}/site`. Nothing is built, so the
  install needs about as much memory as a download. Excalidraw uses it: the site layer of `excalidraw/excalidraw`
  (linux/amd64, published 2026-05-06), found with the registry's manifest API, is a 22 MB download. To move to a newer
  image, read the new manifest's layers and pin the one whose files sit under `usr/share/nginx/html`.
- the launch command is an argv list, never a shell string; its only placeholders are `{port}`, `{data_dir}`,
  `{env_dir}` and, at launch only, `{origin_label}` (the app's own random hostname label, which lets an app such as
  JupyterLab accept writes from its own address and refuse other sites' without a cookie), it must run a program from `{env_dir}`, pass `{port}`, and bind `127.0.0.1`. An optional
  `init_argv` runs once before the first start; `env` adds environment variables (placeholders allowed).
- `[[categories]]` (a key and a title, in display order) are the page's filters; every app's `tags` name one or
  more of them, and every category must hold an app.
- `resources` carries `disk_mb`, `memory_idle_mb` and `memory_in_use_mb` (at least the idle figure).
- a slug is lowercase letters and digits (it becomes the program and app name), and the version must match the
  recipe's pin.

Every entry was verified in a workspace container before it was added (see the comment at the top of
`apps.toml`). To add one: verify the pinned version installs, the argv serves the app at `/` through the
forwarder (a foreign Host and Origin) and can be framed, measure disk and idle memory, draw its icon, and list
its drop-in in `.gitignore` (`catalog_test.py` checks that).

## Adding an app from PyPI or npm

"Add app" installs a package that is not in the catalog. The dialog takes the source (PyPI or npm), the package and
an optional exact version (empty pins the latest), a display name, the start command (shown as it will run), and for
Python the interpreter. "Check" pins the version and estimates the disk without installing anything; confirming
says plainly that nobody reviewed the code, that it runs with the same access as the user's other apps, and where it
comes from, then adds the app and queues its install.

- `added_apps.py` holds the rules -- only a plain registry name (PEP 508 or npm, scoped allowed) at one exact
  version; never a URL, `git+` link, file path, package file, extra or range; the command split once into a list,
  its program a bare name found in the app's own environment, its only placeholders `{port}` (required) and
  `{data_dir}`, and it must listen on 127.0.0.1; a name of its own, from the display name by the catalog's slug rules
  -- and `added-apps.json` in the store's data folder (a format number, atomic writes; a malformed entry is skipped
  with a warning). Nothing is written to the shipped catalog or any tracked file.
- `package_check.py` pins and estimates: Python resolves with `uv pip compile` for the target Python on this
  machine and sums the file uv would choose for each package from the PyPI JSON API, times 3.3 (compressed
  downloads against installed size, measured on the curated installs); npm resolves with
  `npm install --package-lock-only` and sums each package's `dist.unpackedSize`. Lookups are cached
  (`data/.state/app-store/package_lookups.json`) and time-limited; a failure leaves the size unknown, with why.
- `adding.py` checks, adds and removes. An added app becomes an ordinary `CatalogApp` (the generic `added.svg`
  icon, no categories), so the engine installs, repairs, uninstalls and rolls it back exactly like a curated one, and
  its pin never changes. Its drop-in's name is the user's, so it is listed in the repository's local
  `info/exclude` (a block the store owns) rather than the tracked .gitignore. "Remove from store" works once the
  app is not installed, and deletes whatever it left behind.
- The start command may be left empty: "Work it out for me" runs a job in `command_finder.py` that pins the package,
  installs it into a scratch folder (`.command-trials/<job>/` in the store's data folder, always deleted), lists the
  programs it installed (none: not an app), reads each one's `--help` and the package's registry description, and
  asks Claude (`claude -p` through the copied `claude_p.py`, model `FIND_MODEL`) for JSON saying `web_app` with a
  command, `not_an_app`, or `needs_setup`, with a plain explanation. A command is held to the same rules as a typed
  one, then test-launched on a free port until `/` answers. A page that forbids being framed (X-Frame-Options,
  or a frame-ancestors policy without `*`) is a failed try: the workspace shows every app framed, so its window would
  be blank. A screenshot is taken while it answers, inside a frame the way the workspace shows it
  (`screenshot_page.py`, run with the workspace's own Python, which has Playwright); the frame's page is served on
  127.0.0.1 at a port of its own, since a browser refuses `frame-ancestors *` from a blank page and refuses a public
  page framing a local one. A frame the browser refuses is a failed try too, with the browser's reason for Claude.
  Claude may also give short settings files (`files`, at most `MAX_SETUP_FILES`), for an app whose options exist only
  in a file: plain relative paths inside the app's data folder, settings extensions only (`SETUP_FILE_EXTENSIONS`,
  never code). Each try starts from an empty data folder holding just its files; a found command carries them, the
  dialog shows them, and the install writes them (`{port}`, `{data_dir}` filled in) before the first start, never
  over a file already there. A literal brace in a command is
  written twice (`{{`, `}}`), which both the AI and the refusals are told. A failure goes back to Claude
  with the app's own output, up to `MAX_ATTEMPTS` tries. The dialog polls the job, shows the screenshot and asks
  whether it looks right; yes fills the command in and checks it as if it were typed. Each job's raw log (commands,
  output, Claude's answers) is kept at `.command-trials/<job>.log` and linked from the dialog. A package the shipped
  catalog already has is pointed back at the catalog.
- Memory is unknown until the app runs; then the engine's sampler records its process tree's peak over the first
  minutes of each run (`peak_memory_mb`), shown as measured. Unknown memory never blocks an install; low free
  memory reads as Tight.

## Adding an app from a GitHub repository

Planned in full in `data/plans/app-store-add-from-github.md`. **Add app** opens on its first choice, **GitHub**: the
person pastes a link (checked to be a GitHub repository link), and **Work it out in a chat** opens a chat with
`/add-repo-to-store <link>` through the shell's start-with-text, as a template's Install does. That chat follows the
`add-repo-to-store` skill (`.agents/skills/add-repo-to-store/`): it pins the link to a commit, works out an install
file, tries it out, and hands it to the store. The store's half:

- The chat's result is an **install file** (`repo_install_file.py`, `format = 1`): a title and description, the
  repository and the one commit everything was tried at, and the same `recipe`, `launch` and `resources` a catalog
  entry has, held to the catalog's rules. A recipe that installs from the repository must install that same commit.
  Two recipe kinds exist for it besides `github_build` (whose `ref` may now be empty for an unreleased commit) and
  `image_site`: `python_git` (`uv pip install git+<repository>@<commit>[#subdirectory=...]`, or
  `<package_name>[<extras>] @ git+...` when it asks for optional extras) and `node_git` (`npm install
  git+<repository>.git#<commit>`); an app from a commit is versioned by its first 12 hash characters.
- `POST /api/repo-adds` (`{"install_file": {...}, "chat_id": "...", "replaces": ""}`) checks it (400 for a rule a
  field breaks, 422 when its name is taken) and holds it as a **pending add** in `pending-repo-adds.json` (at most 20,
  oldest dropped). Nothing is installed. The trial's screenshot comes with it (`screenshot_png_base64`, a PNG of at
  most 5 MB, which `hand_over.py` sends) and is kept beside the file in `pending-repo-add-screenshots/<id>.png`,
  served at `/api/repo-adds/<id>/screenshot.png`, and dropped with the pending add. The store snapshot lists pending
  adds (`pending_repo_adds`), with the exact install and start commands and the screenshot, so the open page asks
  about the oldest: **Add to store** (`POST /api/repo-adds/<id>/confirm`) makes it an ordinary added app (`source =
  "github"`, the file and the chat kept with it) and queues its install; **Discard** (`DELETE /api/repo-adds/<id>`)
  drops it; **Not now** hides it for the visit.
- A chat tries an install file out before handing it over with `uv run --no-sync python -m app_store.try_install
  <file.json>` from the workspace root (`try_install.py`). It installs the file into `/var/tmp/app-store-trials/` with
  exactly the store's commands and time limits, writes its settings files, runs its setup, starts it on a free port,
  and checks it as a window shows it (it answers, its headers and a framed screenshot do not refuse framing),
  measuring disk, the install's peak memory and the running app's. A memory guard stops any command once less than 15%
  of the workspace's memory is left -- above earlyoom's 10%, so the trial stops first -- and a process earlyoom
  stopped anyway (its shed ledger) also counts as running out of memory. It prints one JSON result (`WORKS`,
  `INSTALL_FAILED`, `START_FAILED`, `REFUSES_FRAMING`, `OUT_OF_MEMORY`, `INVALID_FILE`), keeps the log, screenshot,
  file and result under `data/.tasks/repo-trials/<trial>/`, always deletes the scratch install, and exits 0 only when
  the app works. Measured 2026-11-05: Datasette from a commit works (16 s, 27 MB, 83 MB running); TiddlyWiki from a
  commit works (71 s, 39 MB, 217 MB); Excalidraw v0.18.1 built from source is stopped at about 2.4 GB.

## Updating an installed app

An installed app's page shows its version and how to get a newer one (`update_kind` in the snapshot):

- **A shipped app** (`CATALOG`): its newer version comes with the workspace's own updates; when the catalog's version
  differs from the installed one, the page offers **Update to <version>** (`update_version`).
- **An app added from PyPI or npm** (`REGISTRY`): **Check for updates** asks the registry for the newest version
  (`GET /api/apps/<slug>/newest-version`); the update keeps the same command and settings files at the new pin.
- **An app added from GitHub** (`GITHUB`): **Check for a newer version** opens a chat with
  `/add-repo-to-store <repository> --update <slug>`, which reads the current file
  (`GET /api/added-apps/<slug>/install-file`), finds a later release or commit, tries it out, and hands it over with
  `"replaces": "<slug>"`; the store shows it as **Update <title> to <version>?**.

Confirming queues an `UPDATE` job (`POST /api/apps/<slug>/update`, or the pending add's confirm). It stops the app,
sets its environment aside as `env.previous`, installs the new version into `env`, keeps the data folder, the port and
any existing settings files, rewrites the manifest and drop-in, and starts it. If the install fails or the app does not
answer, the new environment is removed, `env.previous`, the old drop-in and manifest are put back, the app is started
again, and the record stays `INSTALLED` at the old version with the reason in `failure_reason`. An update a restart
interrupts is put back the same way by the reconcile. An added app's entry becomes the new version's only once the
update succeeds (`update_entry` on the record until then).

## Installing

One job at a time, in a background thread (`engine.py`), over records persisted atomically in
`data/.apps/app-store/installs.json`:

1. **Queued**, then **downloading**: the recipe is installed into `data/.apps/app-store/<slug>/env/` (marked
   `.nobackup`: it is rebuildable).
   The program the launch command names must then exist in the environment; if not, the install fails and rolls
   back, listing the programs the package did install.
2. **Configuring**: a free port is picked by the build-app scaffolder's rules (`ports.py`), the app's data
   folder is made (`<slug>/data/`), its generated `app.toml` and `icon.svg` are written beside it, its init runs
   once (in the data folder, where the app itself runs), and `system/supervisord.conf.d/<slug>.conf` is written.
3. **Starting**: `supervisorctl reread` and `update <slug>`, then the store waits for the port to answer; an app
   that never does is rolled back with the end of its own output copied into its install log.
4. **Installed**, with the folder's measured disk recorded beside the estimate; or **failed**, with what the
   install made undone (the log and the data folder are kept) and a plain reason.

The program runs `.venv/bin/app-store-launch <slug>` (`launch.py`) in the user memory band: it registers the generated
manifest and port with `forward_port.py`, then execs the app's argv in its data folder. Installed apps declare
`stop_when_no_windows`, so the shell stops them a minute after their last window closes and starts them again
on the next visit.

Uninstall stops the program, removes its drop-in and its registry row (`forward_port.py --remove`), deletes the
environment, and deletes the app's data only when the user ticks that box.

Every install keeps its raw log at `<slug>/install.log`, served at `/api/apps/<slug>/log` and linked from the
page.

## Surviving restarts and rebuilds

The generated drop-ins are gitignored (one line per catalog slug in `.gitignore`), so a rebuild of the
workspace's code loses them. When the store starts, `InstallEngine.reconcile` makes the workspace match the
records:

- an install a restart interrupted is undone and marked failed;
- an interrupted uninstall, or a queued job, is run;
- an installed app whose drop-in is missing (or stale) gets it back, and supervisord picks it up;
- an installed app whose environment is missing (a restore skips `.nobackup` folders) is queued for a repair,
  which reinstalls the environment and keeps its data.

The store never touches a drop-in or registry row it did not write: an install whose name is taken by another
app is refused.

## Layout

- `catalog.py`, `catalog/` -- the catalog and its schema.
- `engine.py` -- the queue, install, uninstall and reconcile.
- `layout.py` -- where an installed app's files go, and the manifest and drop-in it generates.
- `system.py` -- the small interfaces over the command runner, the package installers, supervisord, the
  registry and the port probe (fakes in `testing.py`).
- `process_memory.py` -- what an installed app's process tree uses right now (`supervisorctl pid`, then `/proc`).
- `room.py` -- free disk and available memory (`oom_priority.meminfo.read_meminfo`, as System Monitor reads it)
  and the verdict.
- `repo_install_file.py`, `repo_adds.py`, `try_install.py` -- an app from GitHub: the install file a chat hands
  over, holding it for confirmation, and trying it out first.
- `ports.py`, `records.py`, `template_sizes.py`, `views.py`, `pages.py` (routes), `main.py`, `launch.py`.
- The template catalog store, the state-file helpers and the page server are Getting Started's
  (`getting_started.template_catalog`, `.state_files`, `.serving`), and the Templates section draws
  Getting Started's own shelves (`getting-started-frontend` in the npm workspace).
- `frontend/` -- the page (Mithril and `@imbue/workspace-ui`), built into `src/app_store/static/`.

## Data

- `data/.apps/app-store/` -- the install records and one folder per installed app.
- `data/.state/app-store/` -- the template catalog's last good copy and the cached template sizes.

## Tests

`uv run pytest` from this directory (backend, 80% coverage floor, with the ratchets), and `npx vitest run`
from `frontend/`.
