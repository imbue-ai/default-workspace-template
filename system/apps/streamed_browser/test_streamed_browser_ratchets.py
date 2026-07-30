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
    # +1 for xinput.py's _XTestKeyboard._RECYCLE_SETTLE_S sleep, ported from
    # Selkies verbatim: after rebinding a RECYCLED overlay keycode the X server
    # applies the mapping synchronously, but xcb-class toolkits refetch keymaps
    # asynchronously and could translate the already-queued press with the old
    # symbol. A 10ms settle is the canonical fix; there is no event to wait on.
    rc.check_time_sleep(_DIR, snapshot(1))


def test_prevent_global_keyword() -> None:
    rc.check_global_keyword(_DIR, snapshot(0))


def test_prevent_bare_print() -> None:
    rc.check_bare_print(_DIR, snapshot(0))


# --- Exception handling ---


def test_prevent_bare_except() -> None:
    rc.check_bare_except(_DIR, snapshot(0))


def test_prevent_broad_exception_catch() -> None:
    # +3, all deliberate boundary catches: two in xinput.py's release paths
    # all deliberate boundary catches: three in xinput.py (a held-key sweep,
    # release_all, and the window-resize finder -- each must tolerate an
    # arbitrary Xlib protocol error from a window/key changing under it, or the
    # user is left with a stuck key or a dead input thread), one in
    # videopipe.py's stop_capture guard, and one in audiopipe.py's stop_capture
    # guard (both native-thread teardowns that have wedged in testing and must
    # never take the service down). Each logs; none silences.
    rc.check_broad_exception_catch(_DIR, snapshot(5))


def test_prevent_builtin_exception_raises() -> None:
    rc.check_builtin_exception_raises(_DIR, snapshot(0))


# --- Import style ---


def test_prevent_inline_imports() -> None:
    rc.check_inline_imports(_DIR, snapshot(0))


def test_prevent_relative_imports() -> None:
    rc.check_relative_imports(_DIR, snapshot(0))


# --- Banned libraries and patterns ---


def test_prevent_asyncio_import() -> None:
    rc.check_asyncio_import(_DIR, snapshot(0))


def test_prevent_dataclasses_import() -> None:
    rc.check_dataclasses_import(_DIR, snapshot(0))
