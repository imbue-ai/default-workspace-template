The scheduled automation-agent prompt in `.mngr/settings.toml` surfaces its chat tab the projects-native way: a single `layout.py open "chat:$MNGR_AGENT_NAME"` that lands in the view the user is looking at, replacing the retired per-named-layout loop (`for L in desktop mobile; do ... --layout "$L" ...; done`) that targeted the deleted `desktop` / `mobile` layouts.

`layout.py`'s module help and its `--view` flag documentation describe the corrected default target: your own view -- the one you were last messaged from, else your chat's project -- before the view the connected client is on.
