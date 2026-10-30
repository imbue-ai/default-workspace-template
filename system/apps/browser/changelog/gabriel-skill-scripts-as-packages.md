The preview test imports update-app's preview tool from the `update_app_skill` package instead of loading `preview_app.py` by path, which is now a thin entry file.

The wake-up the fleet sends a queued agent's chat goes through `message_chat.send()` in-process instead of running `message_chat.py` as a subprocess, so the app depends on `workspace-scripts`. A send that is not delivered is still logged as a warning.
