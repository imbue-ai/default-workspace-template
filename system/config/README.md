# system/config/

Tracked workspace configuration:

- `parent.toml` - The upstream template repository this workspace pulls
  updates from (used by the update-self skill).

Configuration written at runtime (backup settings, GitHub sync) lives in
`data/system/` instead, so it can never be committed by accident.
