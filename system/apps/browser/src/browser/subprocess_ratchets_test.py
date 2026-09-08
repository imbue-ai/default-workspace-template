"""Ratchet confining this project's subprocess spawning to the detached runner.

The browser app runs chromium, Xvfb, pactl and xclip from a background process group on
the workspace's tmux terminal -- see ``detached_subprocess.runner`` for the mechanism.
Chromium and Xvfb are detached and reaped by handle (``ChromeProcess.close``,
``_stop_xvfb``), so supervisord's group kill was never their only way down.

The rules themselves live in ``detached_subprocess.ratchets``; every supervisord program in this
repo applies the same two to its own tree. This file supplies only what is specific here: the
source tree to scan and the allowlist.

Named ``*_test.py`` because that is this repo's convention for a unit test, which a static scan
is. (``test_*_ratchets.py`` is the per-project standard set, which ``system/test_meta_ratchets.py``
requires to be exactly one per project defining identical tests.)
"""

import ast
from pathlib import Path

import pytest
from detached_subprocess.ratchets import (
    BACKGROUND_SPAWN_RULE,
    RAW_SPAWN_RULE,
    called_name,
)
from imbue.imbue_common.ratchet_testing.ratchets import TEST_FILE_PATTERNS
from imbue.imbue_common.ratchet_testing.standard_ratchet_checks import assert_ratchet
from inline_snapshot import snapshot

_SOURCE = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.xdist_group(name="ratchets")

# Two files still spawn attached, for reasons the rule cannot express.
#
# ``xclipboard.py``: ``xclip -i`` forks a background process that keeps serving the selection,
# so its stdout MUST be DEVNULL and never a pipe -- a captured pipe is inherited by that child
# and blocks the parent's communicate() forever, which is the bug set_clipboard's docstring
# records. Both detached entry points capture, so neither can express this call. xclip talks to
# the X server, not a terminal.
#
# ``session.py``: the SHARED pulseaudio daemon is meant to outlive any one browser and be
# adopted by the next through its ``pactl info`` probe, and the group kill on a stop that
# overruns stopwaitsecs is its only reaper -- detaching it would leave it running for good.
#
# Both paths are anchored so the waiver names those two files and not any same-named file
# elsewhere in the tree: Path.match() matches from the RIGHT, so a bare "session.py" would
# waive every session.py under the app. The guard below covers the rest of session.py, which
# the waiver would otherwise leave unwatched.
_ALLOWED_RAW_SPAWN_FILES = (
    "src/browser/xclipboard.py",
    "src/browser/session.py",
    *TEST_FILE_PATTERNS,
)
_SESSION = _SOURCE / "src" / "browser" / "session.py"


def test_prevent_subprocess_spawns_outside_the_detached_runner() -> None:
    assert_ratchet(RAW_SPAWN_RULE, _SOURCE, snapshot(0), _ALLOWED_RAW_SPAWN_FILES)


def test_prevent_background_process_spawns() -> None:
    """Nothing here starts a ConcurrencyGroup process; a new one would need the same detachment,
    asked for by hand."""
    assert_ratchet(BACKGROUND_SPAWN_RULE, _SOURCE, snapshot(0), TEST_FILE_PATTERNS)


def test_the_waived_file_still_spawns_only_the_shared_audio_daemon() -> None:
    """session.py is waived for one spawn, so nothing else would notice a second one appearing.

    Seven of its eight spawns were converted; the waiver exists for the one that cannot be. This
    pins that, so a new attached spawn in the file fails here instead of passing silently.
    """
    module = ast.parse(_SESSION.read_text())
    attached = [
        call
        for call in ast.walk(module)
        if isinstance(call, ast.Call) and called_name(call) in ("run", "Popen", "call", "check_call", "check_output")
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "subprocess"
    ]

    assert len(attached) == 1, (
        f"session.py has {len(attached)} attached spawns; the waiver covers only the shared "
        "pulseaudio daemon. Route a new one through detached_subprocess, or widen this guard "
        "deliberately."
    )
    first_argument = attached[0].args[0]
    assert isinstance(first_argument, ast.List) and isinstance(first_argument.elts[0], ast.Constant)
    assert first_argument.elts[0].value == "pulseaudio", (
        "the one attached spawn session.py is waived for must still be the shared audio daemon"
    )
