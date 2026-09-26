Added `launch_paths` to the manifest and the registry row (`LaunchPath`, `RegistryLaunchPath`, `LaunchPathId`, `LaunchPathValue`): the paths the desktop interface opens windows at, declared beside `actions` until the desktop interface replaces the tabbed shell (`docs/system/blueprint/desktop-interface/contracts.md` section 2).

`default_shortcut` gains an optional `launch`, the desktop interface's spelling of the seeded shortcut, validated against the declared launch paths (or `open` when there are none).
