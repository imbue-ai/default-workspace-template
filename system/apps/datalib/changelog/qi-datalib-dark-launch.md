New app: Datalib, datalib's web UI (`datalib-http`, the Manage screen and the Add/Edit source wizard) over the store the `datalib` skill writes at `data/.skills/datalib`.

- `launch_datalib_http.py`, a standard-library launcher, registers a copy of `app.toml` whose one launch path carries datalib's API token as a preset, so a window opens signed in, and then becomes `datalib-http`. The token is the one at `data/.skills/datalib/system/api-token`, kept across restarts.

- `datalib-http`, and every sync step it starts, runs in the agent-subprocess memory band (`oom_score_adj` 900): under memory pressure datalib is shed before any agent or service.

- The app is not announced to users yet. Its program has `autostart=false`, and an app is listed only once its program has registered it, so no workspace shows a Datalib app until the `datalib` skill starts the program.

- The app is not a uv workspace member (`system/apps/datalib` is excluded in the root `pyproject.toml`).
