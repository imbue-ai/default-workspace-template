"""Ratchet confining this project's subprocess spawning to the detached runner.

This library spawns on behalf of every app that wraps a third-party server (the files
app's dufs, the terminal app's ttyd), each a supervisord program in a background process
group on the workspace's tmux terminal -- see ``detached_subprocess.runner``. The wrapped
server is detached too: ``_forward_signals_to`` hands it every stop signal by handle.

The rules themselves live in ``detached_subprocess.ratchets``; every supervisord program in this
repo applies the same two to its own tree. This file supplies only the source tree to scan.

Named ``*_test.py`` because that is this repo's convention for a unit test, which a static scan
is. (``test_*_ratchets.py`` is the per-project standard set, which ``system/test_meta_ratchets.py``
requires to be exactly one per project defining identical tests.)
"""

from pathlib import Path

import pytest
from detached_subprocess.ratchets import BACKGROUND_SPAWN_RULE, RAW_SPAWN_RULE
from imbue.imbue_common.ratchet_testing.ratchets import TEST_FILE_PATTERNS
from imbue.imbue_common.ratchet_testing.standard_ratchet_checks import assert_ratchet
from inline_snapshot import snapshot

_SOURCE = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.xdist_group(name="ratchets")


def test_prevent_subprocess_spawns_outside_the_detached_runner() -> None:
    assert_ratchet(RAW_SPAWN_RULE, _SOURCE, snapshot(0), TEST_FILE_PATTERNS)


def test_prevent_background_process_spawns() -> None:
    """Nothing here starts a ConcurrencyGroup process; a new one would need the same detachment,
    asked for by hand."""
    assert_ratchet(BACKGROUND_SPAWN_RULE, _SOURCE, snapshot(0), TEST_FILE_PATTERNS)
