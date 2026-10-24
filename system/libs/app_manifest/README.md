# app_manifest

The models behind a workspace app's two descriptions (a third, the OpenAPI document of its HTTP API, is `system/libs/app_schema`):

- **The manifest**, `system/apps/<package>/app.toml`: an app's static
  declarations (name, display name, a one-line description, the path of its
  API document, icon, its memory-shedding priority, whether
  it is critical, whether it stops once no window shows it, whether it is part
  of the workspace itself (which the minds Share tab lists under its System
  group), its supervisord program, the launch paths the desktop opens windows
  at, the shortcut a new desktop is seeded with, the messages from the Imbue
  Studio chrome it takes, and what it owns outside its own directory). The
  schema is `contracts.md`
  section 2 of the desktop interface (`docs/system/blueprint/desktop-interface/`),
  which carries
  section 2 of the workspace app model
  (`docs/system/blueprint/workspace-app-model/`) forward without its instance
  fields.
- **The registry**, `data/.state/apps.toml`: the runtime record of registered
  apps, written only by `system/scripts/forward_port.py` (which copies the
  manifest's fields onto the row at registration and adds the URL and the
  origin label). The row shape is `contracts.md` section 3.

## API

- `app_manifest.manifest`: `AppManifest` (pydantic, `extra = "forbid"`; every
  cross-field rule of the contract is a validator), `LaunchPath`
  (`id`, `label`, `path`, `method` (`GET`, the default, opens a window at the
  path with the params as its query; `POST` posts them to the path and opens a
  window at the path the app answers), `params`, `presets` (fixed name-value
  pairs sent with every launch), and an optional `text_param` or `draft_param`
  naming the one of its params the desktop fills with typed text, sent or
  drafted; `client_id`, `desktop_id`, and `window_path` are reserved for the
  shell's launch envelope and can name neither a param nor a preset; `open` is
  reserved for the root launch path the shell synthesizes for an app that
  declares none), `LaunchPathMethod`,
  `MessageHandler` (`type`, a `MessageType`: a lowercase prefix, `:`, and a
  lowercase kebab-case name, never `shell:`, unique within the manifest; and
  exactly one of `path`, the route under the app's origin the shell posts each
  message of that type to, shaped like a launch path, or `show`, the
  `PageTemplate` of the app's page the shell shows for it, with `showing`, the
  templates of the app's other pages that count as already showing it, only
  beside `show`),
  `DefaultShortcut`
  (`launch`, `mode`), `ShortcutMode`, `AppReference` (`path`, optional `note`),
  `ScopeRules` (`exclude`), `PreviewSpec` (the optional `[preview]` table: how
  a throwaway instance boots, with named free ports, a scratch copy of the
  directories it names, and placeholders in its command, args, and env;
  absent, it is `scaffold_preview_spec(name)`, the build-app convention, so
  every app previews by construction), `load_manifest(path, repo_root=None)`
  (reads, validates, checks the icon file exists beside the manifest, and --
  against the repo root, given or derived from a `system/apps/<package>/app.toml`
  layout -- that every reference exists, sits neither in the app's own
  directory nor in another app's, and goes through no symlinked directory,
  which git never reports a changed file through), `repo_root_for_manifest`,
  `app_package_directory`, and `manifest_icon_path(manifest_path, manifest)`.
- `app_manifest.registry`: `RegistryRow` (absent keys read as the contract's
  defaults; unknown keys are ignored so a newer registration script never hides
  an app from an older reader), `read_registry(path)` (a row that fails
  validation is logged and skipped; an unreadable file raises
  `RegistryReadError`), and `registry_path()` (honours `MINDS_APPS_FILE`,
  default `data/.state/apps.toml` relative to the cwd, exactly like
  `forward_port.py` and `workspace-layout`). `register_app(manifest_path, app_url)` is
  the startup registration every app's entry point calls: it runs
  `system/scripts/forward_port.py --manifest <path> --url <url>` under the
  current interpreter from the repo root, which upserts the app's row from its
  manifest (a re-registration updates the row in place), and raises
  `AppRegistrationError` when the script is missing, fails, or times out.
  `read_origin_label(path, name)` answers
  one app's origin label, or `""` when no such app is registered or the registry
  cannot be read (logged as a warning), for a page that derives another app's
  origin. `SHELL_APP_CONTRACT_PATH` is where the shell's frontend build writes
  the app contract module, which every app serves at `APP_CONTRACT_ROUTE` from
  its own origin (a cross-origin module import carries no cookie, and the
  forwarder refuses it); `SHELL_CONTEXT_MENU_PATH` and `CONTEXT_MENU_ROUTE` are
  the same for the element context menu module built beside it.
- `app_manifest.content_security_policy`: `attach_document_policy(app_or_blueprint,
  overrides=None)` stamps a `Content-Security-Policy` on every HTML response that
  does not set one itself: scripts from the page's own origin plus the page's own
  inline `<script>` blocks (admitted by hash, computed from each response), inline
  styles, images from the page's own origin (plus `data:` and `blob:`), frames of any
  http(s) page, same-origin fetches
  and sockets, no plugins, no `<base>` retargeting, no cross-origin form posts.
  `overrides` replace whole directives for an app that needs more (the browser
  app's `blob:` worklet, the chat's images and media from anywhere, Getting
  Started's thumbnails from the catalog's origin, which `url_origin(url)` gives). `frame-ancestors` is left to the proxy in front of the
  workspace, which appends its own policy. `build_document_policy(html,
  overrides)`, `inline_script_hashes(html)` and `inline_source(text)` (the
  `'sha256-...'` source for one inline script or stylesheet, which the shell's
  parked-port page uses for its stylesheet) are the pieces; a built document
  served with `send_file` is not read and must carry no inline script.
- `app_manifest.scope`: the footprint computation. `compute_app_scope`,
  `compute_skill_scope`, `with_diff_against_base`, and `render_scope_file` build
  the scope file described below; `find_wiring_sections` reads the app's own
  `[program:*]` blocks out of `system/supervisord.conf` and every
  `system/supervisord.conf.d/*.conf` drop-in its `[include]` glob names;
  `find_referencing_manifests(repo_root, target_path)` is the reverse lookup
  from an owned path to the apps that claim it (an app directory with no
  `app.toml` is skipped, and so is a manifest that fails to load, with a warning
  naming it -- `system/test_app_manifests.py` is the loud check for that, so one
  app's stale reference does not block every other creation's footprint).
  `compute_skill_scope` refuses a path that does not exist, because git ignores a
  pathspec that matches nothing and a mistyped path would read as "unchanged".
  `parse_creation_spec`, `compute_creation_scope`, `compute_pass_scope`, and
  `render_pass_scope_file` build the pass scope file behind `app-manifest
  pass-scope`, with `find_fork_point` and `find_departure_point` telling the
  pass's own commits from the lead's, and `read_pass_scope_file` reads one back; `split_against_creations`
  is the diff split both scope files share. `BUILT_IN_EXCLUDES`, `APP_CONVENTIONS`, `SKILL_CONVENTIONS`, and
  `SERVICE_CONVENTIONS` are the fixed lists. Exclude matching is `pathspec`
  gitignore syntax; a failing git command raises `ScopeComputationError` rather
  than reporting an empty diff.
- `app_manifest.selection` and `app_manifest.workspace_graph`: the test
  selection behind `app-manifest select-tests` (below). `workspace_graph` reads
  what the workspace declares about its packages: the uv workspace members and
  npm packages with their consumers (`python_consumers`, `npm_consumers`), the
  suites that run as their own pytest root, and what a `uv.lock` change
  upgraded and who depends on it (`classify_lockfile_change`).
  `scope.list_changed_files` is the diff both `footprint` and `select-tests`
  read, and `select_tests_for_pass_scope` selects from a pass scope file.
- `app_manifest.primitives`: the validated string types (`AppName`,
  `DisplayName`, `LaunchPathId`, `LaunchParamName`, `LaunchPathValue` (rooted with one
  slash, no query string or fragment, nothing a URL would escape), `MessageType`,
  `PageTemplate` (a path with `{field}` placeholders, starting with one `/` or a
  placeholder, optionally ending in a fixed query string, no fragment),
  `PriorityName`, `ProgramName`,
  `RepoRelativePath`, `ReferencePath`, `ExcludeGlob` (no leading `!`: a
  gitignore negation would re-include a built-in exclude), `ReferenceNote`) and
  the name rule shared with `forward_port.py` (a drift test in
  `system/scripts/forward_port_test.py` keeps them identical), with
  `canonical_name_from_title(title)` (the name a user-facing title registers
  as) and `is_name_conflict(candidate_title, taken_names)` (whether a title
  would collide with a name already taken) for apps that mint names from titles,
  and `render_page_template(template, fields)`, the page a `show` handler's
  template names for a message: each placeholder replaced by its string field,
  percent-encoded one path segment at a time, a trailing `/` of the value joining
  the template's following `/` as one slash; it raises `PageTemplateFieldError`
  when a field is missing or not a string or the page is not a rooted path.
- The `app-manifest validate-manifest <path> [--repo-root DIR]` command, for the
  build-app scaffold and tests. Without `--repo-root` the reference location
  checks run against the root the `system/apps/<package>/app.toml` layout
  implies, and a manifest anywhere else skips them.

## The footprint commands

`app-manifest footprint` writes one creation's scope file: everything a review,
test, freshness, or publish pass may treat as that creation's own. Every path in
it is repo-root-relative, and `--repo-root` (default: the current directory, the
same convention `registry_path()` follows) is what they are relative to.

    app-manifest footprint <manifest> [--repo-root DIR] [--diff-base REF [--diff-ref REF]] [--out FILE]
    app-manifest footprint --for-path <path> [--repo-root DIR] [--diff-base REF [--diff-ref REF]] [--out FILE]
    app-manifest references --for-path <path> [--repo-root DIR]

The positional manifest and `--for-path` are alternatives: the first describes
an app, the second a skill (or any other owned path). Without `--out` the JSON
goes to stdout; with it, the parent directories are created.

```json
{
  "creation": {"type": "app", "name": "slack-inbox", "package": "slack_inbox", "manifest": "system/apps/slack_inbox/app.toml"},
  "primary": ["system/apps/slack_inbox/"],
  "wiring": [{"path": "system/supervisord.conf.d/slack-inbox.conf", "sections": ["program:slack-inbox"]}],
  "references": [{"path": ".agents/skills/slack-inbox-refresh", "note": "...", "kind": "skill"}],
  "context": [],
  "conventions": ["system/apps/README.md", ".agents/shared/worker/references/type-app.md", "docs/system/style_guide.md"],
  "exclude": ["system/vendor/**", "data/**", "**/node_modules/**", "**/dist/**", "**/.venv/**"],
  "diff": null
}
```

- `creation.type` is `app` or `skill` (or `service`, from `pass-scope` below); a
  skill's `package` and `manifest` are null and its `name` is the directory's
  name.
- `primary` is what the creation is: the app's package directory, or the
  `--for-path` path. A directory ends in `/`.
- `wiring` is the supervisord sections the app owns: its own
  `program:<program>` block, every `program:<name>-<role>` sidecar, and every
  program the manifest's `[wiring] programs` declares (the browser declares
  `xvfb`, which exists only for it; a declared program with no block is an
  error). The first label of every standalone program is a reserved app name,
  so the sidecar prefix cannot claim an unrelated program. One entry per file a
  block is written in, so a footprint names the file a change would have to
  edit: the browser's own `program:browser` block is in
  `system/supervisord.conf.d/browser.conf` and the `xvfb` it declares is in
  `system/supervisord.conf.d/xvfb.conf`, so its footprint carries both. Empty
  when nothing runs any of them, which is the normal state before an app is
  first registered.
- `references` copies the manifest's entries through, with `kind` derived from
  the path prefix (`skill`, `shared`, `script`, `service`, `doc`, `other`).
- `context` is the surface a creation is judged against: empty for an app; for a
  `--for-path` scope, the primary directory of every app whose manifest
  references that path. Of each context directory only its `app.toml` counts as
  inside the footprint, because a skill's one sanctioned edit outside its own
  directory is the `[[references]]` entry it adds there; a change to the app's
  code is reported outside.
- `conventions` is a fixed list keyed by creation type, not checked for
  existence.
- `exclude` is the built-in globs followed by the manifest's own, deduplicated.
- `diff` is null unless `--diff-base` is given, and then reports the base's full
  sha, the full sha of the `ref` the diff runs to (HEAD, or what `--diff-ref`
  names, so one tree can answer for a range that ends elsewhere -- what a merge
  commit's first parent changed since the fork, say), every file the diff
  changed (from the three-dot form, so what the base branch did after the fork
  is not the creation's change), and that list split in two:
  `inside_footprint`, the changed files under a `primary` path, a `wiring`
  file, a reference, or a `context` entry's `app.toml`, and `outside_footprint`,
  the changed files under none of those. A file matched by `exclude` is in
  neither. A non-empty `outside_footprint` means either a missing reference or a
  change that does not belong on the branch. `fork_point`, `lockfile_base` and
  `incoming_base` are null and `others` and `incoming` empty: they are
  `pass-scope --lead-ref`'s (below).

`app-manifest pass-scope` writes the scope file for a harden pass, which covers
every creation one change touched:

    app-manifest pass-scope --creation <type>:<name> [--creation ...] [--repo-root DIR] [--diff-base REF [--diff-ref REF] [--lead-ref REF]] [--out FILE]

Each `--creation` is `app:<registered name or package directory>`,
`skill:<directory under .agents/skills>`, `service:<package under
system/services>`, or `shared:<path under .agents/shared>` (a shared script or
reference, scoped as `--for-path` scopes it). The file is
`{"creations": [...], "diff": ...}`: one entry per creation, each the scope
`footprint` writes with its `diff` null, and one `diff` for the pass, whose
`inside_footprint` is every changed file some creation's footprint accounts
for (and that creation does not exclude) and whose `outside_footprint` is
every changed file none does. With `--lead-ref` (the branch the pass's branch
was cut from), the diff also tells the pass's own work from other work that
landed in the window, as a worker branched off a shared branch needs: its
`fork_point` is the newest commit of the lead's first-parent history the ref
holds, the pass's own changes are what the ref changes relative to it, every
file of a non-merge commit on the ref's first-parent line since it left the
lead's, and every file of a commit on the lead's first-parent line between the diff base
and the fork point that changed something a footprint accounts for (how the
change being hardened reached the branch; merges are skipped),
`outside_footprint` keeps only those, and `others` lists every changed file
that is neither in a footprint nor the pass's own. A milestone the lead merged
from the pass's branch sits on a second parent there, so it does not move the
fork point; a lead commit the branch merged in does, so what came in with it
is the lead's, while the branch's own commits from before that merge stay its own. `lockfile_base` is the commit a changed `uv.lock` is compared
against to tell what the pass upgraded: the fork point, or the merge base of the
diff base and the fork point when one of the pass's commits before the fork
point changed the lock. `incoming` lists the changed files of the lead's
commits the branch merged in after it left the lead's line (a catch-up): those
from `incoming_base`, the newer of where it left and the window's start, to
the fork point. `incoming_base` is null, and `incoming` empty, when it merged
none within the window. Without `--lead-ref`, `fork_point`, `lockfile_base` and
`incoming_base` are null and `others` and `incoming` empty.
A service has `type` `service`, its directory as
`primary`, and as `wiring` the program named after its package (`host_backup`
runs as `host-backup`) and every `<name>-<role>` one; an app directory with no
`app.toml` yet is scoped the same way. A pass that names one creation twice is
refused, including an app named by both its registered name and its package
directory.

`app-manifest references --for-path <path>` prints one JSON object per line
(`app`, `manifest`, `path`, `note`) for every app whose manifest claims that
path -- directly or as something beneath a referenced directory -- and exits 0
with no output when none does.

`priority` is validated for shape only; whether it names a band is checked
against `oom_priority.bands.SERVICE_BANDS` by `system/test_app_manifests.py`
(for the built-in manifests) and resolved at runtime by the memory backstop,
which treats an unknown band name as `user`.

The registration script itself does not import this library: it runs under a
plain `python3` from every supervisord program line, so it stays stdlib-only
and copies the manifest's fields without applying the rules above. The rules
are applied by `validate-manifest` (which the build-app scaffold runs on the
manifest it writes) and by every reader of the registry.

## Selecting tests

    app-manifest select-tests --diff-base REF [--diff-ref REF] [--repo-root DIR] [--format text|json]
    app-manifest select-tests --path P [--path P ...] [--diff-base REF] [--repo-root DIR] [--format text|json]
    app-manifest select-tests --scope-file FILE [--repo-root DIR] [--format text|json]

`select-tests` prints the commands that can observe a change, one per line, in
the order to run them, each under a comment saying which changed paths called
for it. Every line runs from the repo root. With `--diff-base` the change is
what the ref changed since it forked from the base (the same three-dot diff as
`footprint`); with `--path` it is the paths given, as the working tree holds
them, and `--diff-base` only names the revision a changed `uv.lock` is compared
against. With `--scope-file`, a harden pass's file from `pass-scope
--diff-base REF --lead-ref REF`, the change is every file of its diff except
`others`: what the pass's footprints account for and the pass's own changes,
but not the other work that landed in the window. A changed `uv.lock`
is then compared against the file's `lockfile_base`. Each `incoming` file also
selects whatever it would of the pass's creations' own suites and npm
packages, and nothing else: no suite has yet run those creations against that
work and the pass's together, while the rest of what it reaches was tested
where it was committed. The root project's own `pyproject.toml` or
`conftest.py` runs the suite of each creation in the pass that the root project
collects rather than the full root suite. A skill also runs on the root
project's dependencies, so a package the root project depends on, or a
`uv.lock` upgrade of one, runs the suite of each skill in the pass. A shared
script in the pass belongs to no package or skill, so each of those runs the
full root suite for it, as a change to the script itself does. An incoming
`uv.lock` is compared from `incoming_base` to the fork point; when that cannot
be read, every creation's own suite runs. A `--diff-base` selection that runs to
the checked-out commit refuses to run while the working tree holds uncommitted
or untracked changes, and names them, since the tests would run against
changes the diff leaves out; a `--scope-file` selection always does, and also
refuses a file with no fork point or whose diff does not run to `HEAD`.

Selection reads only what the workspace declares. A path selects:

- its own tests: the package (`system/{libs,services,apps}/<name>`) or skill
  (`.agents/skills/<name>`) it sits in, or, for a collected test file outside
  both, that file alone -- except for a path in an app's frontend, which
  selects only what the npm package bullet below says;
- its consumers: every workspace member that depends on its package, directly
  or transitively (`pyproject.toml` dependencies and dependency groups) -- not
  for a test file, which nothing that depends on the package runs, and not for
  a path in an app's frontend, which is built into the app's own bundle;
- for a path in an app other than a test file or a frontend path, the tests
  beneath each directory the app's manifest references (a referenced skill
  drives the app's surface);
- the app whose manifest references it (`[[references]]`), or whose program a
  supervisord block holds; a supervisord block also selects
  `oom_priority`'s `oom_tag_service_test.py`, which checks that every program
  block names its OOM band;
- for a `uv.lock` change, every member that depends on a package the lock
  upgraded (a package only added selects nothing beyond the member whose
  `pyproject.toml` added it);
- for an npm package, its and its consumers' `npm test`, `npm run lint` and
  `npm run format:check` (and `npm run typecheck` for a package with no build),
  after `npm ci && npm run build`, plus the tests marked `browser` or
  `frontend` of every app whose frontend is among them (and nothing else of
  that app, since no Python code loads its frontend; an app the root suite
  runs runs whole instead, since a marker run over an app none of whose tests
  carry the markers collects nothing, which pytest fails); the npm root's
  configuration and its prebuild script
  (`system/scripts/fetch_mngr_assets.sh`, with the `_mngr_git_auth.sh` it
  sources) select every npm package.

A path in no package, skill or npm package, that is not npm root
configuration, a supervisord block, `uv.lock`, a guard, a collected test file,
markdown under `.agents/` (agent prose, which only the always-run prose checks
and the apps referencing it read), or other markdown those checks read, runs
the full root suite (`uv run pytest` from the repo root), which replaces the
other root-collected commands; so does a change to the root `pyproject.toml` or
`conftest.py`, and a `uv.lock` change that cannot be compared. A `system/*.py`
that is not a test file (a `system/conftest.py`, say) is no guard, so it runs
the full root suite too. So does a path in a package the root project's
`pyproject.toml` depends on, or that one of those packages depends on (other
than a test file or a frontend path), and a `uv.lock` upgrade the root project
depends on directly or through one of those packages: the root project's own
tests, in `system/scripts`, `.agents` and the skills, are its consumers.

A run that can drive a browser comes after `npm ci && npm run build`: an
own-root suite's run that includes its `browser` tests, or its files named on
their own (those tests skip when the bundles are missing). An app's pages
import the shell's built modules (`_static/app_contract.js` and
`_static/context_menu.js`, in the shell's gitignored `static/`), so when the
tree lacks them, as a fresh worktree does, the full root suite and every
root-collected run of an app come after the build too. A tree that has them,
like the live workspace, gets no build for those runs: the modules change only
when a frontend does, which selects the build itself, and a build in the live
workspace rewrites the bundles the running shell serves.

Every change that is not entirely documentation (README and changelog files
anywhere, and other markdown outside `.agents/` and
`system/{scripts,libs,services,apps}/`, where markdown is prose an agent runs;
but not the markdown `system/test_meta_ratchets.py`'s live-prose checks read:
the root `README.md`, `AGENTS.md` and `CLAUDE.md`, and markdown under
`.agents/`, `docs/` and `data/` outside a changelog, blueprint, specs or vendor
directory) also runs the always-run set: the test files in `system/*.py`, the
cross-cutting `system/scripts` guards, and the checks that read every skill's
prose. A listed guard git does not track (an always-run one, or the OOM band
check a supervisord block selects) fails the selection rather than dropping out
of it. A change made only of documentation selects nothing.

`system/apps/chat` and `system/apps/system_interface` run as their own pytest
roots: workspace members with their own pytest configuration, which the root
configuration ignores. Any other directory it ignores (a vendored subtree such
as `system/vendor/tk`) is no suite, and its tests are never selected. The two
apps' suites deselect their `browser` (and chat its `real_claude`) tests by
default. Such an app runs everything (`-m ''`) when its Python changed, its
default run when it was reached as a consumer, and only its tests marked
`browser` or `frontend` (`-m 'browser or frontend'`) when only a frontend it
builds changed; reached both ways, it runs its default run and then only its
`browser` tests (`-m browser`), since the default run holds the `frontend` ones.
A changed test file of such an app is not a change to its Python: nothing else
in the suite imports it, so it runs on its own with every marker (`-m ''`), and
a marker run beside it leaves it out (`--ignore`) so none of its tests runs twice.
The selection reads the markers, never the test files. A run of
only some of such a suite's tests (by marker, or changed test files) passes
`--no-cov` when the suite measures coverage, since only a whole run can reach
its coverage floor. When a run includes the chat suite's `test_no_type_errors`,
the test is deselected and its `ty check` printed as a command of its own, so
the two memory peaks do not stack; a run of only the marked tests includes
neither.

A coupling nothing declares (a script run as a subprocess, a service called
over HTTP, a file one component writes and another reads) is invisible to the
selection. When the consumer is an app, declaring the path in its manifest's
`[[references]]` is what makes a change to the path select it.

