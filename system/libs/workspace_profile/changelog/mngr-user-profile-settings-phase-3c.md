Keybindings become the first account-keyed setting the workspace consumes (user-profile-state spec, phase 3C).

- New `keybindings.py`: the canonical chord grammar (lowercase tokens joined with `+`, modifiers in the fixed ctrl/alt/shift/meta order, named keys from the lowercased `KeyboardEvent.key` vocabulary, `cmd` normalized to `meta`), validation and normalization, and per-assignment resolution across the user's override, the owner's unsent changes, the snapshot, and an app's declared defaults. The grammar vocabulary is pinned against the vendored desktop contract fixture.

- New `manifest.py`: `load_app_keybinding_actions` reads an app's `[[keybindings]]` from its `app.toml` and refuses a default chord off the grammar, plus the shape of each app's `GET /api/keybindings/me` answer and the resolver the apps share. The library now depends on `app-manifest`.

- `overrides.py` gains the atomic writers of the per-user override files and the shell's `unsent.json` (with the same path-shaped-user-id refusal as the loader); errors move to a new `errors.py`.

- `CONSUMED_SETTING_KEYS` gains `"keybindings"`, matching the desktop's updated contract fixture.

- The library's own `identity.py` is gone: the resolution keys on `workspace_layout.identity`'s `RequestIdentity`, the one definition the shell and the apps read, so the library now depends on `workspace-layout`.
