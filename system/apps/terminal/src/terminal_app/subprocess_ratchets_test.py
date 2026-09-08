"""Ratchet confining this project's subprocess spawning to the detached runner.

The terminal app shells out to tmux from a background process group on the workspace's own
tmux terminal -- and tmux is a terminal program, so it is among the likeliest children to
touch one. See ``detached_subprocess.runner`` for the mechanism.

The rules themselves live in ``detached_subprocess.ratchets``; every supervisord program in this
repo applies the same two to its own tree. This file supplies only what is specific here: the
source tree to scan and the allowlist.

Named ``*_test.py`` rather than ``test_*_ratchets.py`` because the latter is the per-project
standard set, and ``system/test_meta_ratchets.py`` requires exactly one of those per project,
defining exactly the same tests as every other.
"""

from pathlib import Path

import pytest
from detached_subprocess.ratchets import BACKGROUND_SPAWN_RULE, RAW_SPAWN_RULE
from imbue.imbue_common.ratchet_testing.ratchets import TEST_FILE_PATTERNS
from imbue.imbue_common.ratchet_testing.standard_ratchet_checks import assert_ratchet
from inline_snapshot import snapshot

_SOURCE = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.xdist_group(name="ratchets")

# ``bin/notify_terminal_session.py`` is a tmux hook, not a child of this service: the tmux
# server runs it, and that server has a session of its own. It is deliberately stdlib-only
# so it can never fail in a way that disrupts tmux, which depending on this library would
# undo.
_ALLOWED_RAW_SPAWN_FILES = ("notify_terminal_session.py", *TEST_FILE_PATTERNS)


def test_prevent_subprocess_spawns_outside_the_detached_runner() -> None:
    assert_ratchet(RAW_SPAWN_RULE, _SOURCE, snapshot(0), _ALLOWED_RAW_SPAWN_FILES)


def test_prevent_background_process_spawns() -> None:
    """Nothing here starts a ConcurrencyGroup process; a new one would need the same detachment,
    asked for by hand."""
    assert_ratchet(BACKGROUND_SPAWN_RULE, _SOURCE, snapshot(0), TEST_FILE_PATTERNS)
