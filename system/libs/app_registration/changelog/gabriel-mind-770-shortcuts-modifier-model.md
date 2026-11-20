Registration copies a keybinding action's `scope` onto the registry row when the manifest gives one, so the shell's catalog sees which actions claim to be workspace-wide.

`forward_port.py --refresh-keybindings <app.toml>...` (and `forward_port.refresh_keybindings`) rewrites only the keybindings of each named app's existing row from its manifest, whether or not the app is running; it never creates a row.
