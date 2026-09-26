# system/config/

Tracked workspace configuration:

- `parent.toml` - The upstream template repository this workspace pulls
  updates from (used by the update-self skill).
- `test_selection_overrides.toml` - The test mappings `app-manifest
  select-tests` cannot derive from the workspace's declarations (see
  `system/libs/app_manifest/README.md`).

Configuration written at runtime (backup settings, GitHub sync) lives in
`data/system/` instead, so it can never be committed by accident.
