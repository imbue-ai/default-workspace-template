The `agentic-browser-fleet` command is renamed `use-browser`; the old name no longer exists.

- `message_chat.py --browser-fleet` wraps a nudge in `<use-browser>...</use-browser>`, where it used `<agentic-browser-fleet>`. The flag and `wrap_browser_fleet_nudge` keep their names.

- The Screen spec, the window-bound resources spec's audience line and `system/apps/README.md` name the new command. Parts of the specs that describe the removed browser app keep the name that app's command had.
