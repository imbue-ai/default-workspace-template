The app's browser tests run in CI again, and some had gone stale or never passed reliably while they did not:

- The long-chat wheel test waits for each tick's motion to land instead of a fixed 50 ms, which failed on slower machines while the chat was still measuring its history. It still fails if a tick makes the transcript jump.

- Five tests started a new chat through the launcher's primary row with nothing typed, which since the main chat landed sends to the main chat instead. They now start it from the chat list's New chat button, which made the separate test of that button with nothing signed in a duplicate of the provider-chooser test, so it is gone.

- The phone Model submenu test for a Pi chat now measures the menu once its slide has landed, not partway through it.

A Pi chat's model list is now read from the `pi` on PATH and cached per install, rather than cached once for the whole process. Before, a process that had read one `pi`'s models kept serving them after a different `pi` came first on PATH; a test that put its own `pi` on PATH after an earlier test had read the image's saw no model row in the menu.

Fixed the app's `ruff check` findings, which CI now enforces: one import block, three lambda assignments in tests, and a `noqa` on seven deliberate no-op hook methods. No behavior change.
