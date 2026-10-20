Every app now has one install: the workspace venv.

- Each built-in app's supervisord program execs its entry point from `.venv/bin` (`.venv/bin/system-interface`, `.venv/bin/chat-app`, ...) instead of a bare name. The bare names already resolved to the venv under supervisord, because `uv run bootstrap` puts `.venv/bin` first on its PATH, so the running apps do not change.

- The build no longer installs each app as its own uv tool. Those tools were a second copy that only an agent's PATH and the update pre-flight ever reached, and a stale one rolled back a good update with `ModuleNotFoundError: No module named 'app_schema'`. The build now removes any such tool an older image still carries (`tool_env.py drop-app-tools`), from both `/root/.local` and the `$HOME` it runs under. It recognizes an app tool by its uv receipt: the tool's own package installed editable from under `system/apps/`.

- `system/config/mngr_plugins.toml` now assigns plugins only to the mngr tool. The chat app gets the harness plugins as dependencies in its own pyproject.

- `contracts.md` section 14, the workspace-app-model plan, `workspace-internals.md` and the app READMEs now describe the single venv install.
