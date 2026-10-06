The library and the `workspace-layout` command know about windows the user popped out into their own Imbue Studio window.

- `WindowPoppedOutError` (a `ShellRefusedOpError`) is the shell's refusal to move such a window; `force` on `WindowArgs`, `PlaceArgs`, and `OpenArgs` overrides it. `DesktopOpAnswer` carries `is_raised_in_own_window`, `is_brought_back`, `unpaired_beside`, and `has_no_desktop_window`, all defaulting for an older shell, and each `InventoryClient` carries its `popped_out` windows.

- Every mutating subcommand takes `--force`, and the ones that can be refused send it. A refusal exits `4` and says `--force` overrides it; summaries add "(raised in its own window ...)", "(brought back from its own window)", why an `open --beside` was left unpaired, and that the client has no desktop window open. `desktops` and `list` show each client's `popped_out` windows.
