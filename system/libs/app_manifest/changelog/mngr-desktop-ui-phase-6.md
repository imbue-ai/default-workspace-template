Phases 6 and 7 of the desktop interface (`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`):

- `register_app` (the startup registration through `forward_port.py --manifest`) and the app naming rule move here from the deleted `app_instances` library, so every app registers through the same call.

- `AppManifest` and `RegistryRow` drop the tabbed shell's `instances`, `instances_url`, and `actions` keys; a manifest carries launch paths, a default shortcut, and a launcher rank only. `forward_port.py` still strips the retired keys from a stale registry row (a `CLEANUP:` note names when that can go).
