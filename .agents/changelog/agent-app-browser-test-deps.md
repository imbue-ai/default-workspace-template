A newly built app's harden pass can now run its browser tests.

- `build-app`'s scaffold gives each new app's own test environment Playwright and pytest-timeout, and registers the `browser` and `frontend` test marks. Before, an app's suite had neither package, so a harden worker's phone browser test failed with `No module named 'playwright'` or `No module named 'pytest_timeout'` and never ran.

- The worker guides `type-app.md` and `web-frontend-testing.md` say the scaffold provides these, and give the one `uv add --dev playwright pytest-timeout` an app scaffolded earlier needs.
