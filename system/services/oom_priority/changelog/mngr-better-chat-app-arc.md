`SERVICE_BANDS` gains `chat` (25, between the shell and the sharing stack) for the chat app that later phases of the workspace app model split out of the system interface.

The backstop event listener resolves a program's band through the app registry: it re-reads `data/.state/apps.toml` (or `MINDS_APPS_FILE`) on every `PROCESS_STATE_RUNNING` event with the new stdlib-only `oom_priority.app_registry` module and maps the program to the `priority` its app's manifest declared (a `SERVICE_BANDS` key; `user` or an unknown band name is the user-service band). A program with no registry row (the services that never register) still resolves by program name exactly as before. `bands.supervisord_program_band` takes the registry's program-to-priority view as a second argument.

The dynamic chat band's comment names the chat app's `ChatOomPrioritizer`.

The README names the chat app's presence route and its message sends as the prioritizer's engagement events in place of the removed `/api/activity`.

`SERVICE_BANDS` gains `terminal-session` at the user-service level (200): the terminal app runs every `terminal-N` tmux session's shell through `oom_tag_service.py terminal-session`, so a terminal tab's shell and whatever it runs are shed before any built-in service and after every agent. Before, the pane inherited the tmux server's fully protected 0 and a runaway build in a terminal outlived the workspace UI.
