# app_manifest

The models behind a workspace app's two descriptions:

- **The manifest**, `system/apps/<package>/app.toml`: an app's static
  declarations (name, display name, icon, whether it serves instances, its
  memory-shedding priority, whether it is critical, its supervisord program, the
  actions it declares, the rail shortcut a new project is seeded with, and what
  it owns outside its own directory). The schema is `contracts.md` section 2 of
  the workspace app model (`docs/system/blueprint/workspace-app-model/`).
- **The registry**, `data/.state/apps.toml`: the runtime record of registered
  apps, written only by `system/scripts/forward_port.py` (which copies the
  manifest's fields onto the row at registration and adds the URL and the
  origin label). The row shape is `contracts.md` section 3.

## API

- `app_manifest.manifest`: `AppManifest` (pydantic, `extra = "forbid"`; every
  cross-field rule of the contract is a validator), `AppAction`,
  `DefaultShortcut`, `ShortcutMode`, `AppReference` (`path`, optional `note`),
  `ScopeRules` (`exclude`), `load_manifest(path, repo_root=None)` (reads,
  validates, checks the icon file exists beside the manifest, and -- against the
  repo root, given or derived from a `system/apps/<package>/app.toml` layout --
  that every reference exists, sits neither in the app's own directory nor in
  another app's, and goes through no symlinked directory, which git never
  reports a changed file through), `repo_root_for_manifest`,
  `app_package_directory`, and `manifest_icon_path(manifest_path, manifest)`.
- `app_manifest.registry`: `RegistryRow` (absent keys read as the contract's
  defaults; unknown keys are ignored so a newer registration script never hides
  an app from an older reader), `read_registry(path)` (a row that fails
  validation is logged and skipped; an unreadable file raises
  `RegistryReadError`), and `registry_path()` (honours `MINDS_APPS_FILE`,
  default `data/.state/apps.toml` relative to the cwd, exactly like
  `forward_port.py` and `layout.py`).
- `app_manifest.scope`: the footprint computation. `compute_app_scope`,
  `compute_skill_scope`, `with_diff_against_base`, and `render_scope_file` build
  the scope file described below; `find_wiring_sections` reads the app's own
  `[program:*]` blocks out of `system/supervisord.conf`;
  `find_referencing_manifests(repo_root, target_path)` is the reverse lookup
  from an owned path to the apps that claim it (an app directory with no
  `app.toml` is skipped, and so is a manifest that fails to load, with a warning
  naming it -- `system/test_app_manifests.py` is the loud check for that, and one
  app's stale reference must not block every other creation's footprint).
  `compute_skill_scope` refuses a path that does not exist, because git ignores a
  pathspec that matches nothing and a mistyped path would read as "unchanged".
  `BUILT_IN_EXCLUDES`, `APP_CONVENTIONS`, and
  `SKILL_CONVENTIONS` are the fixed lists. Exclude matching is `pathspec`
  gitignore syntax; a failing git command raises `ScopeComputationError` rather
  than reporting an empty diff.
- `app_manifest.primitives`: the validated string types (`AppName`,
  `DisplayName`, `ActionId`, `InstancesUrl`, `PriorityName`, `ProgramName`,
  `RepoRelativePath`, `ReferencePath`, `ExcludeGlob` (no leading `!`: a
  gitignore negation would re-include a built-in exclude), `ReferenceNote`) and
  the name rule shared with `forward_port.py` (a drift test in
  `system/scripts/forward_port_test.py` keeps them identical).
- The `app-manifest validate-manifest <path> [--repo-root DIR]` command, for the
  build-app scaffold and tests. Without `--repo-root` the reference location
  checks run against the root the `system/apps/<package>/app.toml` layout implies,
  and a manifest anywhere else skips them.

## The footprint commands

`app-manifest footprint` writes one creation's scope file: everything a review,
test, freshness, or publish pass may treat as that creation's own. Every path in
it is repo-root-relative, and `--repo-root` (default: the current directory, the
same convention `registry_path()` follows) is what they are relative to.

    app-manifest footprint <manifest> [--repo-root DIR] [--diff-base REF] [--out FILE]
    app-manifest footprint --for-path <path> [--repo-root DIR] [--diff-base REF] [--out FILE]
    app-manifest references --for-path <path> [--repo-root DIR]

The positional manifest and `--for-path` are alternatives: the first describes
an app, the second a skill (or any other owned path). Without `--out` the JSON
goes to stdout; with it, the parent directories are created.

```json
{
  "creation": {"type": "app", "name": "slack-inbox", "package": "slack_inbox", "manifest": "system/apps/slack_inbox/app.toml"},
  "primary": ["system/apps/slack_inbox/"],
  "wiring": [{"path": "system/supervisord.conf", "sections": ["program:slack-inbox"]}],
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
- `wiring` is the `system/supervisord.conf` sections the app owns: its own
  `program:<program>` block plus every `program:<name>-<role>` sidecar. Empty
  when the conf runs none of them, which is the normal state before an app is
  first registered.
- `references` copies the manifest's entries through, with `kind` derived from
  the path prefix (`skill`, `shared`, `script`, `service`, `doc`, `other`).
- `context` is the surface a creation is judged against: empty for an app; for a
  `--for-path` scope, the primary directory of every app whose manifest
  references that path. A change under it counts as inside the footprint,
  because a skill's one sanctioned edit outside its own directory is the
  `[[references]]` entry it adds to the owning app's `app.toml`.
- `conventions` is a fixed list keyed by creation type, not checked for
  existence.
- `exclude` is the built-in globs followed by the manifest's own, deduplicated.
- `diff` is null unless `--diff-base` is given, and then reports the base's full
  sha, every file the diff changed (from the three-dot form, so what the base
  branch did after the fork is not the creation's change), and
  `outside_footprint`: the changed files that are neither under a `primary` path,
  nor a `wiring` file, nor under a reference, nor under a `context` entry, nor
  matched by `exclude`. A non-empty `outside_footprint` means either a missing
  reference or a change that does not belong on the branch.

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
