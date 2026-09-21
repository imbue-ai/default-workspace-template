Phases 6 and 7 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`):

- `register_app` (the startup registration through `forward_port.py --manifest`) and the app naming rule move here from the deleted `app_instances` library, so every app registers through the same call.

- `AppManifest` and `RegistryRow` drop the tabbed shell's `instances`, `instances_url`, and `actions` keys; a manifest carries launch paths, a default shortcut, and a launcher rank only. `forward_port.py` still strips the retired keys from a stale registry row (a `CLEANUP:` note names when that can go).

- The manifest and the registry row gain `window_closed_path`, where the shell posts a closed window of the app. `app_manifest.shell_windows` reads the shell's window paths for an app over loopback (`read_app_window_paths`, answering None rather than "no windows" when the shell cannot be read), resolves the shell's URL (`shell_base_url`), and parses a window path's query (`window_query_value`).
