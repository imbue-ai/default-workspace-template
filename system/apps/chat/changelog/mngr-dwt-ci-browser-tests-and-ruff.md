The app's browser tests run in CI again, and two kinds had gone stale while they did not:

- The long-chat wheel test waits for each tick's motion to land instead of a fixed 50 ms, which failed on slower machines while the chat was still measuring its history. It still fails if a tick makes the transcript jump.

- Five tests started a new chat through the launcher's primary row with nothing typed, which since the main chat landed sends to the main chat instead. They now start it from the chat list's New chat button, which made the separate test of that button with nothing signed in a duplicate of the provider-chooser test, so it is gone.

Fixed the app's `ruff check` findings, which CI now enforces: two import blocks, three lambda assignments in tests, and a `noqa` on seven deliberate no-op hook methods. The trailing-comment ratchet rises from 2 to 9 for those `noqa` directives, which it does not yet exempt the way it exempts `# ty: ignore`. No behavior change.
