update-self's "what's new" reads the release sections of the root `CHANGELOG.md`. `changelog-entries` lists the `## [minds-v<version>]` sections of the target's `CHANGELOG.md` that the workspace's base does not have (and `[Unreleased]` when it holds something), next to the changelog entry files added between the two refs, and `--out` writes the sections and then each entry's text to one file, which the worker digests (Step 5 of the worker guide). Its JSON gains `sections`, `added_before_consolidation` and `unlisted`; `added` lists the same paths as before for any update whose two releases have no release section and delete no entry.

The entry listing runs with `--no-renames`, so an entry is not dropped for resembling one the update deletes. A `CHANGELOG.md` or an entry that cannot be read is left out and named under `unlisted`, as is a `CHANGELOG.md` heading that is not a `## [<label>]` one or a label it repeats, and the command still exits 0.

The worker's merge triage takes the target's side for the root `CHANGELOG.md` and `UNABRIDGED_CHANGELOG.md` (saving a workspace's own `CHANGELOG.md` to another path first) and for a changelog entry file the target deletes.

Converting a user package to a standalone project leaves a `uv run` phrase inside the root `CHANGELOG.md` and `UNABRIDGED_CHANGELOG.md` as written.
