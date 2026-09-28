Two test fixes in `test_browser_integration.py`:

- The paste test aims its focus click from the browser window's X geometry. It used Chromium's `outerHeight`, which the stealth-patched Chromium reports at a different made-up size each run, so the click missed the input and the test failed most runs.

- The startup-gate test no longer leaves the shared manager's manifest checkpoint and window-sweep loops running through every later test in the session.
