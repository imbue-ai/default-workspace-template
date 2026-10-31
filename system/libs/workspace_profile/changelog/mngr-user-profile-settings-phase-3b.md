New `workspace-profile` library: reads the workspace profile snapshot a client (the Imbue Studio desktop, or the hosted web chrome) writes at `data/.state/profile/account.json`, loads the workspace's own per-user override files and the shell's unsent changes under `data/system/profile/`, and resolves account-keyed settings for a request identity (user-profile-state spec, phase 3B).

The snapshot carries no setting keys yet (keybindings arrive with phase 3C); its format and keys are pinned against the desktop's vendored contract fixture.
