New entry file `system/scripts/update_dispatcher_tick.py`: one minute of the update dispatcher, which the `update-self-dispatch` cron line runs from the workspace root with the system `python3` and no venv. Its library, `system/libs/workspace_update_control`, joins the bare search paths and the bare type check, and `system/libs/README.md` lists it.

New library `system/libs/workspace_backup_setup` joins the workspace, with `pytest-executable-tmp` in the dev dependencies for its and the update library's stub programs.

The desktop interface's contracts gain section 5.7, the workspace routes (`/api/workspace/...`), and the state docs say which files those routes now write: the profile snapshot, the user's timezone, the update schedule with `last-start.json`, and `data/.state/backup_setup/`.

The tag gate (`.github/workflows/release-pin-gate.yml`) gains a job that fails a `minds-v*` tag whose tree does not hold `workspace_routes_api_frozen/<tag>.json` as a copy of its `workspace_routes_api.json`. A tree from before the workspace routes passes it.
