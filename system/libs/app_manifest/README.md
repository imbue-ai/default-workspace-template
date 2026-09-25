# app_manifest

The models behind a workspace app's two descriptions:

- **The manifest**, `system/apps/<package>/app.toml`: an app's static
  declarations (name, display name, icon, its memory-shedding priority, whether
  it is critical, its supervisord program, the launch paths the desktop opens
  windows at, the shortcut a new desktop is seeded with, and what it owns
  outside its own directory). The schema is `contracts.md` section 2 of the
  desktop interface (`docs/system/blueprint/desktop-interface/`), which carries
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
  `forward_port.py` and `layout.py`). `register_app(manifest_path, app_url)` is
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
  forwarder refuses it).
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
  `BUILT_IN_EXCLUDES`, `APP_CONVENTIONS`, and `SKILL_CONVENTIONS` are the fixed
  lists. Exclude matching is `pathspec` gitignore syntax; a failing git command
  raises `ScopeComputationError` rather than reporting an empty diff.
- `app_manifest.selection` and `app_manifest.workspace_graph`: the test
  selection behind `app-manifest select-tests` (below). `workspace_graph` reads
  what the workspace declares about its packages: the uv workspace members and
  npm packages with their consumers (`python_consumers`, `npm_consumers`), the
  suites that run as their own pytest root, and what a `uv.lock` change
  upgraded and who depends on it (`classify_lockfile_change`).
  `scope.list_changed_files` is the diff both `footprint` and `select-tests`
  read.
- `app_manifest.primitives`: the validated string types (`AppName`,
  `DisplayName`, `LaunchPathId`, `LaunchParamName`, `LaunchPathValue` (rooted with one
  slash, no query string or fragment, nothing a URL would escape),
  `PriorityName`, `ProgramName`,
  `RepoRelativePath`, `ReferencePath`, `ExcludeGlob` (no leading `!`: a
  gitignore negation would re-include a built-in exclude), `ReferenceNote`) and
  the name rule shared with `forward_port.py` (a drift test in
  `system/scripts/forward_port_test.py` keeps them identical), with
  `canonical_name_from_title(title)` (the name a user-facing title registers
  as) and `is_name_conflict(candidate_title, taken_names)` (whether a title
  would collide with a name already taken) for apps that mint names from titles.
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

- `creation.type` is `app` or `skill`; a skill's `package` and `manifest` are
  null and its `name` is the directory's name.
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
  change that does not belong on the branch.

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

`select-tests` prints the commands that can observe a change, one per line, in
the order to run them, each under a comment saying which changed paths called
for it. Every line runs from the repo root. With `--diff-base` the change is
what the ref changed since it forked from the base (the same three-dot diff as
`footprint`); with `--path` it is the paths given, as the working tree holds
them, and `--diff-base` only names the revision a changed `uv.lock` is compared
against.

A path selects:

- its own tests: the package (`system/{libs,services,apps}/<name>`) or skill
  (`.agents/skills/<name>`) it sits in, or, in the flat script directories
  (`system/scripts`, `.agents/shared/scripts`), the tests paired with it by
  filename (`<stem>.py`, `<stem>.sh` and `<stem>/` pair with `<stem>_test.py`
  and `test_<stem>*.py`; a deleted script needs no pair);
- its consumers: every workspace member that depends on its package, directly
  or transitively (`pyproject.toml` dependencies and dependency groups), and
  the unpackaged scripts that import one of its modules (not for a test file,
  which nothing that depends on the package runs);
- for a `uv.lock` change, every member that depends on a package the lock
  upgraded (a package only added selects nothing beyond the member whose
  `pyproject.toml` added it);
- for an npm package, its and its consumers' `npm test`, `npm run lint` and
  `npm run format:check` (and `npm run typecheck` for a package with no build),
  after `npm ci && npm run build`, plus the browser tests of every app whose
  frontend is among them;
- the app whose manifest references it, or whose supervisord block it holds;
- for a path in an app other than a test file, the tests beneath each
  directory the app's manifest references (a referenced skill drives the
  app's surface);
- every test file that names it;
- what the override file says.

Every change that is not entirely documentation (README and changelog files
anywhere, and other markdown outside `.agents/` and `system/{scripts,libs,services,apps}/`,
where markdown is prose an agent runs) also runs the
always-run set: `system/*.py` and the cross-cutting guards the override file
lists. A change made only of documentation selects nothing.

`system/apps/chat` and `system/apps/system_interface` run as their own pytest
roots, which deselect their browser tests (the `release` marker) by default.
Such an app runs with them (`-m ''`) when the app itself changed, and without
them when it was reached as a consumer. A run of only some of such a suite's
files (its browser tests, or a test file that names a changed path) passes
`--no-cov` when the suite measures coverage, since only a whole run can reach
its coverage floor. The chat suite's `test_no_type_errors`
is deselected and its `ty check` printed as a command of its own, so the two
memory peaks do not stack.

A path none of this classifies brings in the full root suite, and is named in
an `# unclassified` block at the end. That is the signal to decide which suites
can observe the path and record it in the override file.

### The override file

`system/config/test_selection_overrides.toml` holds what the declarations
cannot show: `always_run` (the cross-cutting guards), `[[consumer]]` entries
(`paths` globs, the `suites` they select, and a `note`; an empty `suites` says
the always-run set covers the paths), and `[[integration]]` entries for tests
that drive a real installed tool (`test`, the `paths` that select it, and a
`note`). Paths are gitignore-style globs over repo-root-relative paths; a suite
is a test file or a suite directory. A suite that does not exist fails the
selection rather than selecting nothing. `test_repo_test_selection.py` holds the real tree to the
mapping: every tracked path that is not documentation must classify, and every
suite the file names must exist. CI runs it on every change, and a workspace
runs it whenever the selector or the override file changes.

