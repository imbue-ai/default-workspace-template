# Testing a web frontend

Testing guidance for anything that serves a web UI: an app the workspace
built, or one of the workspace's own apps (the shell, the chat, the terminal).
Your own guide adds the specifics: where the app lives, its stack, its test
entry points.

## Drive an isolated instance, never the live one

Exercise the app **in-process** or against a **throwaway** instance on an
alternate port -- never the app's registered live
port. Drive the Flask app with its test client (`app.test_client()`), or launch a
disposable threaded Werkzeug server (`run_simple(..., threaded=True)`). Never
restart, curl, or "reveal" the live service; revealing the change is the lead's
job after merge.

## Assert on real behavior

Assert on markers that are true if and only if a route behaves correctly --
status, the rendered content, the raw-data/source affordance, and the empty and
overflow states -- not just that the route returned `200`. Add Playwright
coverage wherever the value is in the rendered UI rather than the JSON, driving
it against the isolated instance. In a suite the repo-root `conftest.py`
reaches, use its `module_browser` fixture (and `phone_context` for a
phone-sized touch context): it launches Fortress, the workspace's browser,
which is installed before any agent starts, and it fails a test that asks for
pytest-playwright's `page` / `context` / `browser` instead. A user-built app's
suite is its own pytest root, which that conftest does not reach: its browser
test adds Playwright to the app's own dev group and launches Fortress itself.
The chat app's and the shell's suites are their own pytest roots too, and
their own conftests point `page` / `context` / `browser` at Fortress, so use
those there. Where a conftest provides the browser, do not write your own
launch fixture. Never `playwright install` a managed browser, and never skip
browser tests on browser presence -- a browser that cannot launch must fail
the run. Give every browser test a `pytest.mark.timeout(120, func_only=False)`
marker: under the repo-root conftest it is the only per-suite setup a browser test
needs, since the repo-wide 10-second default does not cover a browser launch, and
a user-built app's suite honours it through the `pytest-timeout` in its own dev
group (`type-app.md`'s "On a phone"). Mark every browser test
`pytest.mark.browser`, and every test that reads an app's frontend source
`pytest.mark.frontend`: when only the chat app's or the shell's frontend
changed, the test gate runs just those two kinds of its tests (any other app
runs whole). The chat app's and the shell's own suites skip their browser
tests by default: run them with `-m ''` (everything) or `-m browser --no-cov`
(only them; each suite fails a run under its coverage floor, so a partial run
needs `--no-cov`). The test gate passes `-m ''` when the app's Python changed.

## Look at the rendered page

**If your creation renders a frontend, you MUST look at the actual rendered page
-- not just assert on the DOM.** A clean build and passing Playwright assertions
prove the markup and wiring exist; they do NOT prove the page *looks* right --
layout, spacing, alignment, overflow/truncation, color/contrast, z-order, and
whether your change broke something visually elsewhere. Before you report `done`,
capture screenshots of every page and state your change affects (driving the same
isolated Playwright instance; `page.screenshot(...)`, and
`page.set_viewport_size(...)` if layout is width-sensitive), then **actually open
and view those images and judge them with your own eyes.** Fix and re-screenshot
until correct. These development screenshots are a manual check, not a committed
test.
