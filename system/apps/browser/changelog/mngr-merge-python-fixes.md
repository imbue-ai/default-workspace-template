The `telemetry_watch` usage line runs it with `uv run --no-sync`.

The preview test imports update-app's preview tool from the `update_app_skill` package instead of loading `preview_app.py` by path, which is now a thin entry file.

The wake-up the fleet sends a queued agent's chat goes through `message_chat.send()` in-process instead of running `message_chat.py` as a subprocess, on threads of its own so a slow send never holds up a browser's start or stop, and the app depends on `workspace-scripts`. A send that is not delivered is still logged as a warning.

The project requires Python 3.12 or newer (was 3.11), matching the workspace floor: every workspace already runs 3.12, and the lock already required it.

The CDP proxy's refusals and the fleet CLI's no-agent error now tell an agent to run `uv run --no-sync agentic-browser-fleet ...`. The bare `agentic-browser-fleet` reached an agent's shell only through the browser app's own uv tool, which the workspace no longer installs; the CLI now runs from the workspace venv, as the `agentic-browser-fleet` skill already said.
