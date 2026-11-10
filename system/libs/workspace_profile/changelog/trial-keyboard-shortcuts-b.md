The chord grammar refuses Ctrl+Alt+Del (`alt+meta+delete`, `REFUSED_CHORDS`) on every platform: `normalize_chord` raises for it, a keybindings value naming it is a problem, and a manifest declaring it as a default fails to load. The snapshot contract test pins the list against the desktop's fixture.

`is_workspace_wide_chord` says whether a chord may fire anywhere in the workspace (a modifier other than Shift alone, or a function key), and `find_invalid_default_chords` (renamed from `find_off_grammar_default_chords`) also rejects a workspace-wide action's default that typing could trigger.
