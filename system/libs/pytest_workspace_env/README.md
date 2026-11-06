# pytest-workspace-env

A test-only pytest plugin that makes a test run in a workspace behave like
CI: it clears the environment an mngr agent runs with, and it turns a skip
into a failure where the environment is supposed to be complete.

## The agent environment is cleared

Every mngr agent runs with variables CI never has: mngr's own (`MNGR_AGENT_ID`,
`MNGR_AGENT_NAME`, `MNGR_HOST_DIR`, `MNGR_AGENT_ROLE`, ..., and the
`MAIN_CLAUDE_SESSION_ID` its claude agents export), Imbue Studio's
(`MINDS_CHAT_ID`, ...), the code-guardian stop hook's settings, Claude Code's
own session variables (`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, ...), the tmux
session the agent runs in (`TMUX`, `TMUX_PANE`), and the per-agent directories
the agent types point their harness at. A test that reads one of them without
setting it passes in CI and fails when an agent runs the suite, which is how
update-self's test gate runs it.

So the plugin removes every variable whose name starts with `MNGR_`, `MINDS_`,
`CODE_GUARDIAN_`, `REVIEWER_` or `CLAUDE_`, plus `CLAUDECODE`,
`MAIN_CLAUDE_SESSION_ID`, `TMUX`, `TMUX_PANE`, `LLM_USER_PATH`,
`ORIGINAL_CLAUDE_CONFIG_DIR` and `PI_CODING_AGENT_DIR`, before any conftest is
imported. Nothing the tests run can see them: not a module read at import time,
not a test, not a subprocess a test starts. A test that needs one sets it
itself (`monkeypatch.setenv`). The session header lists what was cleared.

CI runs its pytest passes with a few of these variables set to fake values, so
a pass that fails to load the plugin turns red there instead of in a user's
workspace.

## A skip fails where the environment must be complete

Many tests skip when the host lacks something a provisioned workspace has:
the Fortress Chromium build and Xvfb, the pinned `claude` binary, `mngr`,
`jq`, `restic`, `tmux`. That is right on a laptop and wrong in the CI job that
runs the suites inside the workspace image: there, a skip means the image is
missing something, and a green run would hide it.

With `DWT_REQUIRE_WORKSPACE_ENV=1` in the environment, a test that skips is
reported as failed (a `pytest.skip()` call from the test body) or as an error
(a `skipif` condition, which skips at setup), with the skip's own reason in the
message. There is no way to exempt a test: one that cannot run inside a
workspace is rewritten until it can. Without the variable a skip stays a
skip. The session header says when it is on.

The plugin acts on the skips of collected tests: a `skipif`, a `pytest.skip()`
from the test body or from a fixture. A module that skips itself at collection
(`pytest.importorskip(...)`, or `pytest.skip(allow_module_level=True)` at module
level) is outside its reach, so skip per test rather than per module.

## Where it runs

It is registered through the `pytest11` entry point, so every pytest run in the
workspace venv loads it: the root pass and the isolated `system/apps/chat` and
`system/apps/system_interface` passes alike. Each of those three pytest
configurations names it in `required_plugins`, so a pass that cannot load it
stops before collecting anything.
