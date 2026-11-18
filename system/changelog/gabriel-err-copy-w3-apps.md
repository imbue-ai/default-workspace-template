The Screen spec (`docs/system/specs/screen/spec.md`) quotes the Screen's new stopped and setting-up wording: "The Screen was stopped." after Quit, "The Screen stopped unexpectedly." after a crash or a memory shed, and "The Screen is still being set up in this workspace." or "The Screen couldn't be set up in this workspace." while it installs.

`uv.lock` records the `testing` extra of `app-manifest` (pytest and playwright, for its shell stand-in test helpers) and the test suites that now list it: chat, the shell, Getting Started and the Screen.
