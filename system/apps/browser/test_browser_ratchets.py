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
    # +2 for telemetry_watch.py, the STANDALONE terminal telemetry dashboard (a diagnostic
    # CLI, not daemon/library code): its reader (reconnect-and-ingest) and painter
    # (redraw-on-a-timer) are two genuine infinite loops that run until the user quits with
    # Ctrl-C. The daemon-side telemetry paths deliberately avoid while-true (the resource
    # sampler ticks via Event.wait; the firehose socket loops on a `connected` flag like the
    # cast/stream handlers).
    rc.check_while_true(_DIR, snapshot(2))


def test_prevent_time_sleep() -> None:
    # 2 for the boot-a-server integration tests (test_browser_integration.py): they
    # start the real threaded Werkzeug server on an ephemeral port and poll for server
    # readiness and for a state transition over a real socket -- the only way to verify
    # the disconnect-as-lease + cast-WS contract that the in-process Flask test client
    # cannot exercise. +4 for OFF-LOOP blocking waits on the pixelflux/pcmflux media path:
    # session.py's Xvfb-readiness poll in _spawn_xvfb and PulseAudio-daemon-readiness poll
    # in _ensure_pulse_daemon (both run via asyncio.to_thread, so neither blocks the loop),
    # xinput.py's XTEST device-recycle settle (on its own capture thread), and
    # mediastream.py's _await_clipboard_owned
    # poll in the Flask request thread (confirms xclip has claimed the X selection before
    # injecting Ctrl+V, so paste success is truthful). All are real hardware/display/daemon
    # settles, not event-loop sleeps.
    # +3 for chrome_launcher.py, which owns the Chromium process directly now that
    # browser-use is gone: the debug-port poll (Chromium publishes DevToolsActivePort
    # asynchronously after exec), and two waits in reap_orphan while a SIGTERM/SIGKILL
    # takes effect. All three run in a worker thread via asyncio.to_thread, so none of
    # them blocks the event loop -- they are process settles, like the Xvfb one above.
    rc.check_time_sleep(_DIR, snapshot(9))


def test_prevent_global_keyword() -> None:
    rc.check_global_keyword(_DIR, snapshot(0))


def test_prevent_bare_print() -> None:
    # +3 for telemetry_watch.py's ``sys.stdout.write`` calls. This is the standalone terminal
    # dashboard whose entire job is to render to the terminal; writing the frame to stdout is
    # correct output, not stray debug printing (the rule's target). Daemon/library code still
    # uses loguru.
    rc.check_bare_print(_DIR, snapshot(3))


# --- Exception handling ---


def test_prevent_bare_except() -> None:
    rc.check_bare_except(_DIR, snapshot(0))


def test_prevent_broad_exception_catch() -> None:
    # All 18 are best-effort boundaries at the async loop / device / X11 edges, each
    # marked `# noqa: BLE001` with a reason; none silences a real bug.
    #  * 1 in session.py run_agent(): a browser-use Agent run can fail many ways (LLM,
    #    CDP, navigation); we catch broadly so any failure is surfaced to the user's chat
    #    instead of being swallowed as an unretrieved-task exception (re-logged + reported).
    #  * 9 in session.py at CDP boundaries (get_tabs / get_or_create_cdp_session /
    #    activateTarget / createTarget / closeTarget / navigate during restore): browser-use
    #    drives Chromium over cdp-use, whose errors are NOT a fixed subclass of the
    #    _BROWSER_ERRORS tuple, so a narrow catch could let a CDP hiccup wedge the single
    #    event loop. These are bounded (asyncio.wait_for) and best-effort by design.
    #  * 4 in xinput.py: XTEST injection is
    #    best-effort and must never raise into the /stream request thread.
    #  * 3 in xclipboard.py: the XFixes monitor
    #    runs on its own thread against a python-xlib connection whose errors are an open
    #    set; a monitor-thread or callback crash must be logged, not silent or fatal.
    #  * 1 in audiopipe.py: stopping the native
    #    pcmflux capture handle during teardown must never raise up into the sender thread.
    #  * +4 for the passive telemetry subsystem, all `# noqa: BLE001` boundaries that must
    #    never propagate: 2 in telemetry.py (emit must never break the stream; the resource
    #    sampler must never crash the daemon) and 2 in telemetry_watch.py (the CLI reconnects
    #    on any socket drop and exits cleanly on any read error).
    #  * +1 in stream_conductor.py: StreamConnection.wake() nudges a sender loop and is
    #    best-effort by contract (a failed wake must never fault the caller flipping the
    #    active viewer), so it swallows any wake-callback error and logs at debug.
    #  * +2 in window_guardian.py: the per-viewer window-pinning thread runs on its own X
    #    connection and must never die on a transient X error -- one guards opening the display
    #    (a failure just disables the guard) and one guards each tick (logged at debug, loop
    #    continues). ConnectionClosedError is caught narrowly above these.
    #  * +5 for the CDP layer that replaced browser-use, all `# noqa: BLE001` boundaries
    #    where a raise would be worse than a degraded read: 2 in cdp_client.py (the read
    #    loop treats ANY failure as "the socket is gone", and `ping` turns any failure into
    #    a liveness verdict rather than an exception -- crash detection must return a bool,
    #    never throw), 2 in cdp_proxy.py (one bad agent session must not take the whole
    #    server down; a dead upstream must answer discovery with 502 rather than 500), and
    #    1 in session.py's page-count helper, which backs the proxy's last-page guard and
    #    must fail open rather than wedge the agent's socket.
    #  * +1 in session.py's start(), around the fleet's CDP connect. websockets' handshake
    #    errors and the JSON/KeyError from resolving the debug URL are all OUTSIDE
    #    _BROWSER_ERRORS, so without this they escape the launch task entirely -- stranding
    #    the browser in `init` while it holds its name and a slot against the fleet cap
    #    forever, with Chromium and Xvfb still running and the viewer stuck on "Starting".
    #    It re-raises as BrowserStartupError, which is what actually tears the browser down.
    rc.check_broad_exception_catch(_DIR, snapshot(31))


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
    # +1 (same MISFIRE shape) for telemetry.py's `try: import psutil except ImportError`
    # optional-dependency guard -- a module-level import the regex flags only because the
    # try body is indented; the resource sampler degrades gracefully when psutil is absent.
    # Down from 3: two of the flagged imports were browser-use's, and went with it.
    rc.check_inline_imports(_DIR, snapshot(1))


def test_prevent_relative_imports() -> None:
    rc.check_relative_imports(_DIR, snapshot(0))


# --- Banned libraries and patterns ---


def test_prevent_asyncio_import() -> None:
    # The CDP client, the CDP proxy, and the per-browser ownership state
    # machine are all asyncio-native and run on ONE background event loop. Four files
    # rely on asyncio: session.py (the state machine + run loop), loop_bridge.py (the
    # single sync<->async quarantine loop -- the one place run.py's old asyncio usage
    # moved to), and the two test modules that drive session.py with asyncio.run.
    # runner.py itself is now synchronous Flask and no longer imports asyncio (it
    # reaches the loop only through the bridge), so the count holds at 4 despite the
    # FastAPI->Flask swap. Mirrors the system_interface lib's async-WS ratchet.
    # +1 for telemetry_watch.py, the standalone terminal dashboard: it's an async
    # ``websockets`` client that reads the firehose and renders concurrently -- a separate
    # CLI process, not daemon code, where asyncio is the natural fit.
    # +3 for the CDP layer that replaced browser-use: cdp_client.py (the fleet's own
    # connection), cdp_proxy.py (the agent's gated endpoint -- a websockets server), and
    # cdp_proxy_test.py which drives them. Both modules live on the SAME single background
    # loop behind loop_bridge; this is not a second event loop.
    rc.check_asyncio_import(_DIR, snapshot(8))


def test_prevent_dataclasses_import() -> None:
    rc.check_dataclasses_import(_DIR, snapshot(0))

