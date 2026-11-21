The `github-sync` service now keeps each user app's private dev repository current. On every tick it splits the history of each linked app (`git subtree split`, never `--rejoin`, never a commit) and pushes it fast-forward-only to the app's `<name>-dev` repository, recording what it pushed in `data/.state/app_sync/<name>.json`; a lost state file only costs a split and a push. Each dev repository's visibility is checked on its own, and one that is public stops only its own pushes. The status file and `github-sync status` name every app with its state (`pushed`, `up_to_date`, `no_dev_repository`, `not_linked`, `unmodified`, `not_private`, `visibility_unknown`, `push_refused`, `failed`).

`github-sync sync-app <name>` runs that for one app on request, whether or not sync is enabled for the workspace.

New for callers: `github_sync.repositories` (create and lock down a GitHub repository through latchkey, which the publish-template script now calls), `github_sync.app_history` (whether an app is linked or modified, and its split), and `git_environment_through_gateway`. The library now depends on `app-manifest`.

`github_sync.repositories` also gains `set_repository_topics` and `hint_for_push_failure` (what a refused push most likely means), which moved here from the publish-template scripts so that the app publish uses the same two.
