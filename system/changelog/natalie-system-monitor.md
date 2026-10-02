Wired in the new System Monitor built-in app (`activity`):

- Its frontend joins the npm workspace (`system/package.json`, the lockfile) and its build output is ignored.

- The image build pre-copies its `pyproject.toml` and `frontend/package.json`, as it does for every built-in, and `uv.lock` gains the `activity` package.

- `system/test_app_manifests.py` lists it among the built-ins, with `stop_when_no_windows` and launcher rank 50.

- The built-in tables in `docs/system/blueprint/desktop-interface/contracts.md`, `docs/system/blueprint/workspace-app-model/contracts.md` (with its preview) and `docs/system/specs/stop-when-no-windows.md`, `system/apps/README.md` and the `workspace_ui` README describe it.

- `docs/system/specs/system-monitor.md` describes the app's design: where each figure comes from, how processes are credited and the closing point computed, its cost, trust model, failure handling, testing, limitations and next steps.
