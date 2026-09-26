The `update-app` skill no longer tells an agent to copy an app's data into `/tmp`. A workspace container's `/tmp` is a tmpfs, so a copy there is held in memory against the container's limit and earlyoom cannot free it; copying one app's 8.3 GB store there got the whole workspace killed. The scratch copy an agent tests a change against is now made by `serve_isolated_instance.py up --copy data=data/.apps/<name> --env '<PKG>_DATA_DIR={copy:data}'`, which puts it on disk in the instance's own directory and deletes it on `down`, so there is no separate `cp` or `rm` to get right.

- New `.agents/shared/scripts/copy_app_data.py`: `snapshot --app <name> --label <label>` copies an app's data aside before a change to the live store, under `data/.state/app-data-snapshots/<app>/<label>/`, and `drop` removes it. It replaces the skill's `cp -r data/.apps/<name> /tmp/<name>-pre-<change>`.

- Every copy of app data, whether from `--copy` (which previews use too) or from `snapshot`, now checks that it fits first. A copy that would leave less than 2 GB free on its disk is refused before anything is written, with a message saying what to do instead. A copy that fails part way is removed rather than left holding the space.

- `minds-api` saves backup exports to `/var/tmp` instead of `/tmp`, and `file-sharing` points large files at `/var/tmp`.
