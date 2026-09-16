Publishing a template from a workspace that has run update-self no longer ships the workspace's other creations. The template base was taken to be the `update-self:` merge commit, but that merge holds the upstream template plus everything the workspace had built before the update. A user who published one app got a second app inside the template, and the published repo also carried the workspace's full commit history up to the update, none of it secret-scanned. The base is now the merge's upstream parent, resolved by a new shared `.agents/shared/scripts/resolve_template_base.py`, which also answers where a workspace was created (`--origin`).

- `build_template.sh` refuses a base that descends from the workspace's `Initial workspace commit` (exit 5), so a merge commit or `HEAD` can no longer be used as a base, however it was picked.

- Before asking the user to confirm, publish-template lists what the assembled template contains beyond the base. It stops if anything falls outside the confirmed scope.

- update-published-template stops before updating a template that already carries the workspace's own history, and tells the user why an update cannot remove it.

- migrate-workspace's baseline diff uses the same rule. Before, for a source that had run update-self, it left out everything the user built before their last update.

- A data path opted into a template now actually ships in it. The snapshot was staged with `git add -A`, which honours the template's gitignore of all of `data/`, so every `--data-include` path was overlaid, named in the manifest as shipping, and then silently dropped from the commit. Each opted-in path is now added by force on top of that.

- A workspace's version history now records its own creation. The `created from` line walked back to the oldest bootstrap marker in the history, which in a full-history clone belongs to the template repo or to the mind a template was published from -- a workspace created today read as created a month earlier, from a release it never ran.

- The assembly script's refusal messages told you to create the throwaway worktree at the base ref. That worktree has none of the files being published and none of the workspace's own history, so following the advice failed; they now name `HEAD`.
