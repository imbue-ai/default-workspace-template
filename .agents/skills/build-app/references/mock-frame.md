# The mock frame

While any of a new app is not real, the user sees it only inside the mock
frame: the `<name>-sketch` app, which puts a "UI Sketch" banner around the
app's pages and a note under the banner saying what is not real yet. While the
frame is up, `uv run --no-sync workspace-layout list` lists `<name>-sketch` in
the app's place, and the bare app stays out of the launcher.

Read this when a round needs more than SKILL.md's two step-4 calls: the note
changed, the frame's window is closed, the app has become real, or you are
changing a framed app from another skill.

## The rule

Never open or refresh the app itself (`verify_app.py --open` or `--refresh`,
`workspace-layout open <name>`) while the frame is up. The window the user has
is the frame's. Every restart of the app registers it as an ordinary app again,
so every call that restarts it ends with one of the `mock_frame.py` commands
below, which puts the frame back.

## The commands

`MOCK_FRAME` stands for
`uv run --no-sync .agents/skills/build-app/python/build_app_skill/mock_frame.py`.
Chain each after the restart with `&&`, in the same tool call:

| When | Command |
|---|---|
| First time: frame the app and open the frame beside the chat | `MOCK_FRAME up --app <name> --note "<what is not real yet>" --open` |
| A round that changes nothing real | `MOCK_FRAME refresh --app <name>` |
| What is real has changed | `MOCK_FRAME up --app <name> --note "<the new note>" --refresh` |
| The frame has no window open | `MOCK_FRAME up --app <name> --open` |
| The app is real | `MOCK_FRAME down --app <name>` |

- `refresh` re-frames the app with the frame's current note and reloads the
  frame's window. With no frame up, it reloads the app's own windows.
- `up` without `--note` keeps the current note.
- `down` closes the frame's window, removes the frame, and puts the app back in
  the launcher as an ordinary app. Run it before the call that opens the app:

  ```bash
  uv run --no-sync .agents/skills/build-app/python/build_app_skill/mock_frame.py down --app <name> && uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart --marker "<expected-heading-or-text>" --open
  ```
