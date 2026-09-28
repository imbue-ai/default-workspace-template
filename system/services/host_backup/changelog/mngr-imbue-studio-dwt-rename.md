Backup documentation and messages now call the desktop app Imbue Studio -- it is Imbue Studio that writes `data/.secrets/restic.env` and runs `restic init` from outside the workspace. The desktop app is now called Imbue Studio, and the noun for the thing you talk to is now "your agent" rather than "your mind".

The `minds-v<X>` tag scheme and the `backup-update: minds-v<X>` commit-subject convention are unchanged: the desktop updater matches on both, so renaming them needs a coordinated release rather than a text edit.
