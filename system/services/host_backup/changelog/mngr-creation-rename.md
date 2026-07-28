The workspace vocabulary and tree were renamed around "creations": users make apps (opened as tabs), skills (an automation is a skill run on a schedule), data, and customizations.

Moved from system/libs/ to system/services/ (a tab-less background service). The [program:host-backup] block, pyproject registration, and uv run host-backup entry point are unchanged; the minds desktop client probes the new path alongside the old ones.
