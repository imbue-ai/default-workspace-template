# app_manifest

The models behind a workspace app's two descriptions:

- **The manifest**, `system/apps/<package>/app.toml`: an app's static
  declarations (name, display name, icon, whether it serves instances, its
  memory-shedding priority, whether it is critical, its supervisord program, the
  actions it declares, and the rail shortcut a new project is seeded with). The
  schema is `contracts.md` section 2 of the workspace app model
  (`docs/system/blueprint/workspace-app-model/`).
- **The registry**, `data/.state/apps.toml`: the runtime record of registered
  apps, written only by `system/scripts/forward_port.py` (which copies the
  manifest's fields onto the row at registration and adds the URL and the
  origin label). The row shape is `contracts.md` section 3.

## API

- `app_manifest.manifest`: `AppManifest` (pydantic, `extra = "forbid"`; every
  cross-field rule of the contract is a validator), `AppAction`,
  `DefaultShortcut`, `ShortcutMode`, `load_manifest(path)` (reads, validates,
  and checks the icon file exists beside the manifest), and
  `manifest_icon_path(manifest_path, manifest)`.
- `app_manifest.registry`: `RegistryRow` (absent keys read as the contract's
  defaults; unknown keys are ignored so a newer registration script never hides
  an app from an older reader), `read_registry(path)` (a row that fails
  validation is logged and skipped; an unreadable file raises
  `RegistryReadError`), and `registry_path()` (honours `MINDS_APPS_FILE`,
  default `data/.state/apps.toml` relative to the cwd, exactly like
  `forward_port.py` and `layout.py`).
- `app_manifest.primitives`: the validated string types (`AppName`,
  `DisplayName`, `ActionId`, `InstancesUrl`, `PriorityName`, `ProgramName`) and
  the name rule shared with `forward_port.py` (a drift test in
  `system/scripts/forward_port_test.py` keeps them identical).
- The `app-manifest validate-manifest <path>` command, for the build-app
  scaffold and tests.

`priority` is validated for shape only; whether it names a band is checked
against `oom_priority.bands.SERVICE_BANDS` by `system/test_app_manifests.py`
(for the built-in manifests) and resolved at runtime by the memory backstop,
which treats an unknown band name as `user`.

The registration script itself does not import this library: it runs under a
plain `python3` from every supervisord program line, so it stays stdlib-only
and copies the manifest's fields without applying the rules above. The rules
are applied by `validate-manifest` (which the build-app scaffold runs on the
manifest it writes) and by every reader of the registry.
