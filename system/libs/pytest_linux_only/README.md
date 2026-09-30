# pytest-linux-only

A test-only pytest plugin that stops a test run on macOS before it collects
anything.

This repo's suites are written for the Linux container a workspace runs in. On
a Mac some cannot install at all (Linux-only wheels, the Fortress browser) and
others fail for reasons that are the Mac's, not the code's (bash 3.2, socket
path limits, no `/proc`). A run there costs minutes and tells little, so the
plugin ends it at configure time with a message saying to push and let CI run
the tests, or to run them in a workspace.

Set `DWT_ALLOW_MACOS_TESTS=1` to run anyway, for pure-Python code whose tests
do not touch any of that; expect macOS-only failures elsewhere.

It is registered through the `pytest11` entry point, so every pytest run in the
workspace venv loads it: the root pass and the isolated `system/apps/chat` and
`system/apps/system_interface` passes alike.
