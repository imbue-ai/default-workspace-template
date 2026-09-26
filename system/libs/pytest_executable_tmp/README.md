# pytest-executable-tmp

A test-only pytest plugin that makes sure a session's temporary files live
somewhere a file written there can be run.

Many of the workspace's suites stand a stub in for a real tool (`tmux`, `mngr`,
`latchkey`, ...) by writing an executable under `tmp_path` and putting it first
on `PATH`. A workspace container mounts `/tmp` as a tmpfs, and Docker mounts a
bare `--tmpfs` `noexec`. There the stub cannot run, so `PATH` lookup
skips it and the *real* tool runs instead. For the terminal app's tests that means the
user's live tmux server, whose sessions the tests then kill. A CI runner's
`/tmp` is executable, so CI remounts it `noexec` before its pytest steps: a
pytest root that stops loading this plugin fails there too.

At configure time the plugin runs a one-line script under the temp root pytest
is about to use:

- It runs: nothing changes.
- It does not, and the root is pytest's default: temporary files move to
  `/var/tmp`, for `tmp_path`, `tempfile` and, through `TMPDIR`, every
  subprocess a test starts. It is off the backed-up home volume, and short
  enough that a unix socket a test binds under `tmp_path` stays within the
  `AF_UNIX` path limit. The session header says so.
- It does not, and the root was chosen explicitly (`--basetemp` or
  `PYTEST_DEBUG_TEMPROOT`), or no candidate works: the session stops before any
  test runs, rather than letting stubs fall through to real tools.

It is registered through the `pytest11` entry point, so every pytest run in the
workspace venv loads it: the root pass and the isolated `system/apps/chat` and
`system/apps/system_interface` passes alike.
