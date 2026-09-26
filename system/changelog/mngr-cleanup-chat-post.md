Launch paths that start something are POSTs now, so no page URL has a side effect any more (issue #646; the spec is `docs/system/blueprint/post-launch-paths/`).

An app's manifest may declare `method = "POST"` on a launch path, along with fixed `presets` sent with every launch and a `draft_param` (the counterpart of `text_param` for text that is drafted rather than sent). `forward_port.py` copies the three onto the registry row. The names `client_id`, `desktop_id`, and `window_path` are the shell's envelope and are refused as param or preset names.

`layout.py open --launch` works unchanged: a POST launch path is posted the `--param` values by the shell and the window opens at the page the app answers. Windows no longer carry `is_settling`; `desktops` and `list` stop printing it.

The desktop-interface, pinned-taskbar-entries, launcher-and-getting-started, and window-bound-resources documents are amended where the spec's section 13 says.
