# Testing a web frontend

Shared testing guidance for any creation that serves a web UI -- a scaffolded
app (`type-app.md`) or the system interface
(`type-system-interface.md`). Apply it alongside the universal contract in
`harden-creation.md`; your creation reference adds the specifics (where the app
lives, its stack, its test entry points).

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
it against the isolated instance. Use pytest-playwright's `page` / `context` /
`browser` fixtures: the repo-root `conftest.py` already points them at
Fortress, the workspace's browser, which is installed before any agent starts.
Do not write your own launch fixture, do not `playwright install` a managed
browser, and never skip browser tests on browser presence -- a browser that
cannot launch must fail the run. The only per-suite setup a browser test needs
is a `pytest.mark.timeout(120, func_only=False)` marker, since the repo-wide
10-second default does not cover a browser launch.

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
