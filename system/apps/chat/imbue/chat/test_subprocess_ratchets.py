"""Ratchet confining this app's subprocess spawning to the detached runner.

The chat app runs in a background process group on the workspace's tmux terminal, so a child
that inherits that terminal can stop the whole service when it is killed -- see
``detached_subprocess.runner`` for the mechanism. This app is where the exposure bites hardest:
it is the one that shells out to the harness CLIs, and ``claude`` opens ``/dev/tty`` directly
even with its stdio redirected.

The rules themselves live in ``detached_subprocess.ratchets`` because the system interface is a
supervisord service with the same exposure and applies the same two rules to its own tree. This
file supplies only what is specific to the chat app: the source tree to scan, the allowlist, and
the guard behind ``agent_manager.py``'s exemption.

Lives outside ``test_ratchets.py`` because that file is the per-project standard set.
"""

from pathlib import Path

import pytest
from detached_subprocess.ratchets import BACKGROUND_SPAWN_RULE
from detached_subprocess.ratchets import RAW_SPAWN_RULE
from detached_subprocess.ratchets import called_name
from detached_subprocess.ratchets import find_undetached_background_spawns
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.ratchets import TEST_FILE_PATTERNS
from imbue.imbue_common.ratchet_testing.standard_ratchet_checks import assert_ratchet

_SOURCE = Path(__file__).parent.parent.parent
_AGENT_MANAGER = _SOURCE / "imbue" / "chat" / "agent_manager.py"

pytestmark = pytest.mark.xdist_group(name="ratchets")

# The test files stand up their own children to exercise unrelated machinery (a git repo to
# discover, a real mngr to drive), outside the terminal shape these rules are about. The PTY
# auth flows' ``pexpect.spawn`` is isolated by other means: it puts its child in a new session
# on a pty of its own.
_ALLOWED_RAW_SPAWN_FILES = TEST_FILE_PATTERNS
# agent_manager.py's long-running ``mngr observe`` child cannot go through the runner, which
# runs a command to completion; it asks ConcurrencyGroup for the same detachment directly.
_ALLOWED_BACKGROUND_SPAWN_FILES = ("agent_manager.py", *TEST_FILE_PATTERNS)


def test_prevent_subprocess_spawns_outside_the_detached_runner() -> None:
    assert_ratchet(RAW_SPAWN_RULE, _SOURCE, snapshot(0), _ALLOWED_RAW_SPAWN_FILES)


def test_prevent_background_process_spawns_outside_the_one_that_detaches_itself() -> None:
    assert_ratchet(BACKGROUND_SPAWN_RULE, _SOURCE, snapshot(0), _ALLOWED_BACKGROUND_SPAWN_FILES)


def test_the_allowlisted_background_spawns_detach_themselves() -> None:
    """agent_manager.py is exempt from the rule above, so nothing else would notice a spawn there
    going back to attached."""
    spawns, undetached = find_undetached_background_spawns(_AGENT_MANAGER)

    assert spawns, "agent_manager.py's background spawn is gone or renamed, so this guard checks nothing"
    assert not undetached, "\n".join(
        f"the {called_name(spawn)} call on line {spawn.lineno} of agent_manager.py must pass "
        "is_detached_from_terminal=True; without it the child inherits this service's controlling "
        "terminal and can stop the whole service when killed"
        for spawn in undetached
    )
