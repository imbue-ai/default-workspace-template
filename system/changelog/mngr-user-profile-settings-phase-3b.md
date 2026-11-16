User-profile-state phase 3B groundwork, workspace side:

- The bug-report diagnostics collector now carries the two new per-workspace update records beside the schedule, `skipped-target.json` and `dismissals.json` (the skipped update target and update-row dismissals the Imbue Studio app moves into the workspace).

- `system/scripts/fetch_mngr_assets.sh` vendors one more asset: the desktop's `workspace_profile_contract.json`, which pins the workspace profile snapshot's format and keys for `workspace-profile`'s cross-repo contract test.

- `data/.state/README.md`, `data/system/README.md`, and the blueprint contracts docs document the new paths: the profile snapshot at `data/.state/profile/account.json`, the workspace's own settings layer under `data/system/profile/`, the `verification_disabled` key in `backup.toml`, and the previously undocumented `data/.state/update-apply/` records.

- The manage-routines skill's prose reflects that the container timezone now comes from `data/.state/user_timezone` (written once at create) rather than a boot-time fetch.
