Test runs now load the new `pytest-linux-only` plugin, which stops a run on macOS before it collects anything and says to push and let CI run the tests (set `DWT_ALLOW_MACOS_TESTS=1` to run anyway).
