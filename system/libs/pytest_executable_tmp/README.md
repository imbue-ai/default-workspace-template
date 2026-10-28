# pytest-executable-tmp

A test-only pytest plugin that stops a session whose temporary files could not
be run.

Many of the workspace's suites stand a stub in for a real tool (`tmux`, `mngr`,
`latchkey`, ...) by writing an executable under `tmp_path` and putting it first
on `PATH`. Under a `noexec` mount the stub cannot run, so `PATH` lookup skips
it and the *real* tool runs instead. For the terminal app's tests that means the
user's live tmux server, whose sessions the tests then kill. A workspace
container mounts `/tmp` with `exec` for this reason (Docker mounts a bare
`--tmpfs` `noexec`).

At configure time the plugin runs a one-line script under the temp root pytest
is about to use: `TMPDIR`'s, or the one chosen with `--basetemp` or
`PYTEST_DEBUG_TEMPROOT`. When it runs, nothing changes. When it does not, the
session stops before any test runs, rather than letting stubs fall through to
real tools.

It is registered through the `pytest11` entry point, so every pytest run in the
workspace venv loads it: the root pass and the isolated `system/apps/chat` and
`system/apps/system_interface` passes alike.
