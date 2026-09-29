# pytest-workspace-env

A test-only pytest plugin that turns an environment skip into a failure where
the environment is supposed to be complete.

Tests marked `browser` or `real_claude` need what only a provisioned workspace
has: the Fortress Chromium build and Xvfb, or the pinned `claude` binary. On a
host without them they skip, which is right on a laptop and wrong in the CI job
that runs the suites inside the workspace image: there, a skip means the image
is missing something, and a green run would hide it.

With `DWT_REQUIRE_WORKSPACE_ENV=1` in the environment, a test carrying one of
those markers that skips is reported as failed (a `pytest.skip()` call from the
test body) or as an error (a `skipif` condition, which skips at setup), with the
skip's own reason in the message. Every other skip
is left alone, and without the variable the plugin does nothing. The session
header says when it is on.

It is registered through the `pytest11` entry point, so every pytest run in the
workspace venv loads it: the root pass and the isolated `system/apps/chat` and
`system/apps/system_interface` passes alike.
