"""Ratchet confining this app's subprocess spawning to the detached runner.

The system interface runs in a background process group on the workspace's tmux terminal, so a
child that inherits that terminal can stop the whole service when it is killed -- see
``detached_subprocess.runner`` for the mechanism. Nothing here spawns a child that needs the
terminal, so every subprocess must be detached from it.

The rules themselves live in ``detached_subprocess.ratchets`` because the chat app is a
supervisord service with the same exposure and applies the same two rules to its own tree. This
file supplies only what is specific to the system interface: the source tree to scan and the
allowlist.

Lives outside ``test_ratchets.py`` because that file is the per-project standard set.
"""

from pathlib import Path

import pytest
from detached_subprocess.ratchets import BACKGROUND_SPAWN_RULE
from detached_subprocess.ratchets import RAW_SPAWN_RULE
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.ratchets import TEST_FILE_PATTERNS
from imbue.imbue_common.ratchet_testing.standard_ratchet_checks import assert_ratchet

_SOURCE = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.xdist_group(name="ratchets")

# The test files stand up their own children to exercise unrelated machinery (a git repo to
# discover, a server to talk to), outside the terminal shape these rules are about. Nothing in
# this app's own source is exempt: the one spawn it makes, update_staleness.py's git read, goes
# through the runner like everything else.
_ALLOWED_SPAWN_FILES = TEST_FILE_PATTERNS


def test_prevent_subprocess_spawns_outside_the_detached_runner() -> None:
    assert_ratchet(RAW_SPAWN_RULE, _SOURCE, snapshot(0), _ALLOWED_SPAWN_FILES)


def test_prevent_background_process_spawns() -> None:
    """The system interface starts no long-running child of its own; the chat app owns the one
    ``mngr observe``. A new one here would need the same detachment, asked for by hand."""
    assert_ratchet(BACKGROUND_SPAWN_RULE, _SOURCE, snapshot(0), _ALLOWED_SPAWN_FILES)
