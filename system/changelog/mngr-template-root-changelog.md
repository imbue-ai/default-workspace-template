The template has a root `CHANGELOG.md`, a summary that starts with an empty `## [Unreleased]` section and is laid out for one `## [minds-v<version>] - <date>` section per release, and a root `UNABRIDGED_CHANGELOG.md` for the verbatim entries. The per-change entry files stay in their `changelog/` folders until they are consolidated into the two files.

The changelog gate (`system/scripts/workspace_scripts/check_changelog_entries.py`) asks no entry of a branch whose name starts with `mngr/changelog-consolidation`.
