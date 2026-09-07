# Manual scroll verification

Playwright-driven manual verification for the transcript smooth-scroll engine
(docs/system/specs/transcript-smooth-scroll.md). Deliberately NOT pytest tests:
real-browser scroll timing is inherently flaky in CI, but these scripts are the
fastest way to re-verify the must-pass scenarios against a real tool-heavy
transcript after touching the engine.

Lives under system/scripts (not inside the system_interface project) because
these are manual dev tools: the project's code ratchets (no time.sleep, no bare
print, no unittest.mock) rightly bar such patterns from app and test code, but
they are the substance of a manual driver.

Usage (from system/apps/system_interface, with the frontend built via
`cd frontend && npm run build`):

    uv run python ../../scripts/scroll_verification/serve_scroll_fixture.py 8642 <real-session.jsonl> /tmp/scroll-fixture &
    uv run python ../../scripts/scroll_verification/verify_scroll.py /tmp/scroll-fixture/claude_config/projects/fixture-project/scrollfix-session-001.jsonl

Pick any large real Claude session JSONL as the fixture (e.g. from
~/.claude/projects). The verifier prints PASS/FAIL for each scenario: fill
convergence, wheel-during-streaming, anchored-position stability (streaming and
backfill-above), scrollbar drags/jumps, persistence restore, and the
send-snaps-to-FOLLOW transition. The ?debug=scroll page exposes
window.__scrollTrace and window.__scrollDebugState for ad-hoc digging.
