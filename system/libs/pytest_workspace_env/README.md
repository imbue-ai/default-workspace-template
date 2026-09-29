# pytest-workspace-env

A test-only pytest plugin that turns a skip into a failure where the
environment is supposed to be complete.

Many tests skip when the host lacks something a provisioned workspace has:
the Fortress Chromium build and Xvfb, the pinned `claude` binary, `mngr`,
`jq`, `restic`, `tmux`. That is right on a laptop and wrong in the CI job that
runs the suites inside the workspace image: there, a skip means the image is
missing something, and a green run would hide it.

With `DWT_REQUIRE_WORKSPACE_ENV=1` in the environment, a test that skips is
reported as failed (a `pytest.skip()` call from the test body) or as an error
(a `skipif` condition, which skips at setup), with the skip's own reason in the
message. The one exception is a test marked `may_skip_in_workspace`: its skip
holds inside a workspace too (a check that needs a non-root user, say), so it
is left alone. Without the variable the plugin does nothing. The session header
says when it is on.

It is registered through the `pytest11` entry point, so every pytest run in the
workspace venv loads it: the root pass and the isolated `system/apps/chat` and
`system/apps/system_interface` passes alike.
