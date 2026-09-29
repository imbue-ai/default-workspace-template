The browser's real-Chromium paste test no longer fails on x86_64 hosts. It aimed its setup click from Chromium's reported outer window height, which the x86_64 Fortress build makes up on every launch, so the click usually missed the input and the test failed before any paste. It now takes the window's position and size from X. On x86_64 hosts, a workspace update whose gate runs the root test suite (for example minds-v0.7.3 to minds-v0.8.0) failed on this test.

The init-gate test (`test_init_gate_blocks_ownership_but_not_read_only_or_create`) now runs in GitHub CI. It needs no Chromium but carried two leftover real-Chromium skip marks.

`ruff check` no longer reports two unused variables in the browser integration tests.
