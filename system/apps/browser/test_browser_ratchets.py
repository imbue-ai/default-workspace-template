from pathlib import Path

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc
from inline_snapshot import snapshot

_DIR = Path(__file__).parent


# --- Code safety ---


def test_prevent_todos() -> None:
    rc.check_todos(_DIR, snapshot(0))


def test_prevent_exec_usage() -> None:
    rc.check_exec(_DIR, snapshot(0))


def test_prevent_eval_usage() -> None:
    rc.check_eval(_DIR, snapshot(0))


def test_prevent_while_true() -> None:
    rc.check_while_true(_DIR, snapshot(0))


def test_prevent_time_sleep() -> None:
    # 5 = 2 boot-a-server integration tests + 3 production sleeps that run OFF the event
    # loop (so they honor the no-block-the-loop spirit of this ratchet):
    #  - test_browser_integration.py (x2): start the real threaded Werkzeug server on an
    #    ephemeral port and poll for readiness + a state transition over a real socket --
    #    the only way to verify the disconnect-as-lease + cast-WS contract the in-process
    #    Flask test client cannot exercise (mirrors system_interface's boot-server tests).
    #  - session.py (x2): the per-browser Xvfb + PulseAudio readiness polls, run in a worker
    #    thread via asyncio.to_thread (never on the loop) while the display/sink come up.
    #  - xinput.py (x1): the Selkies keymap-settle after rebinding a recycled overlay
    #    keycode, on the dedicated input thread. All three are thread sleeps, not loop blocks.
    rc.check_time_sleep(_DIR, snapshot(5))


def test_prevent_global_keyword() -> None:
    rc.check_global_keyword(_DIR, snapshot(0))


def test_prevent_bare_print() -> None:
    rc.check_bare_print(_DIR, snapshot(0))


# --- Exception handling ---


def test_prevent_bare_except() -> None:
    rc.check_bare_except(_DIR, snapshot(0))


def test_prevent_broad_exception_catch() -> None:
    # 8 broad catches, every one an intentional isolation boundary that MUST NOT let a
    # stray error kill a long-lived thread or a best-effort side effect:
    #  - session.py (x1): run_agent() -- a browser-use Agent run fails many ways (LLM, CDP,
    #    navigation); caught at the task boundary so any failure reaches the user's chat.
    #  - xinput.py (x4): the XTEST input thread -- per-key release in the held-key sweep and
    #    release_all, the best-effort Ctrl+V paste, and the window resize. Xlib raises
    #    assorted protocol errors; none may tear down input for the whole browser.
    #  - xclipboard.py (x3): the XFixes clipboard-monitor thread -- opening the display, the
    #    select loop, and the on_change callback. A monitor-thread crash must be logged,
    #    not silent, and a bad callback must not kill the monitor.
    rc.check_broad_exception_catch(_DIR, snapshot(8))


def test_prevent_builtin_exception_raises() -> None:
    rc.check_builtin_exception_raises(_DIR, snapshot(0))


# --- Import style ---


def test_prevent_inline_imports() -> None:
    # +2 (MISFIRE) for names.py's module-level `try: from imbue.mngr... except
    # ImportError: <local fallback>` block. This is the canonical optional-dependency
    # pattern (the mngr name generator is reused when importable, with a tiny local
    # word-pair generator as the fallback so the browser lib stands alone). The two
    # imports are at MODULE level, not "inline within functions" -- the rule's actual
    # target -- but the regex matches them because a `try` body is indented. Done once
    # at import time (a `_generate` callable is bound), so the importability check costs
    # nothing per call. Making the regex distinguish module-level try/except ImportError
    # from function-inline imports risks missing real violations, so this is bumped.
    rc.check_inline_imports(_DIR, snapshot(2))


def test_prevent_relative_imports() -> None:
    rc.check_relative_imports(_DIR, snapshot(0))


# --- Banned libraries and patterns ---


def test_prevent_asyncio_import() -> None:
    # browser_use, the Playwright async API, and the per-browser ownership state
    # machine are all asyncio-native and run on ONE background event loop. Four files
    # rely on asyncio: session.py (the state machine + run loop), loop_bridge.py (the
    # single sync<->async quarantine loop -- the one place run.py's old asyncio usage
    # moved to), and the two test modules that drive session.py with asyncio.run.
    # runner.py itself is now synchronous Flask and no longer imports asyncio (it
    # reaches the loop only through the bridge), so the count holds at 4 despite the
    # FastAPI->Flask swap. Mirrors the system_interface lib's async-WS ratchet.
    rc.check_asyncio_import(_DIR, snapshot(4))


def test_prevent_dataclasses_import() -> None:
    rc.check_dataclasses_import(_DIR, snapshot(0))

