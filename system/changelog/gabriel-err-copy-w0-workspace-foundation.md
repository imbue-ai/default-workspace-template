The shared frontend library reads every failed request one way and words it in one place, with the status and server text behind "See details" (see `system/libs/workspace_ui/changelog/gabriel-err-copy-w0-workspace-foundation.md`).

- The desktop-interface contract and the post-launch-paths plan say the shell writes no sentence of its own for a failed launch: it passes on the app's `detail` for any status, and the app's own `user_facing_message` and `reportable` when the app sent them, and the frontend words the rest.
