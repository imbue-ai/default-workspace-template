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
message. There is no way to exempt a test: one that cannot run inside a
workspace is rewritten until it can. Without the variable the plugin does
nothing. The session header says when it is on.

The plugin acts on the skips of collected tests: a `skipif`, a `pytest.skip()`
from the test body or from a fixture. A module that skips itself at collection
(`pytest.importorskip(...)`, or `pytest.skip(allow_module_level=True)` at module
level) is outside its reach, so skip per test rather than per module.

It is registered through the `pytest11` entry point, so every pytest run in the
workspace venv loads it: the root pass and the isolated `system/apps/chat` and
`system/apps/system_interface` passes alike.
