"""Project-specific ratchet confining subprocess spawning to ``subprocess_runner.py``.

The system interface runs in a background process group on the workspace's tmux terminal, so
a child that inherits that terminal can stop the whole service when it is killed -- see
``subprocess_runner.py`` for the mechanism. Nothing here spawns a child that needs the
terminal, so every subprocess must be detached from it. These are allowlist-by-file rather
than counts, because a single new attached spawn reintroduces the whole failure mode.

There are two rules because there are two ways to spawn. Run-to-completion commands go through
``run_detached_command``. Long-running background processes cannot -- they need
``ConcurrencyGroup`` -- so they ask it for the detachment themselves; the ``mngr observe``
child reached the runner that way and was invisible to a rule looking only for the raw call.

Lives outside ``test_ratchets.py`` because that file is the per-project standard set, and
these rules are specific to this project.
"""

from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.common_ratchets import RatchetRuleInfo
from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RegexPattern
from imbue.imbue_common.ratchet_testing.core import check_regex_ratchet

_SOURCE = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.xdist_group(name="ratchets")

_RAW_SPAWN_RULE = RatchetRuleInfo(
    rule_name="subprocess spawns outside subprocess_runner.py",
    rule_description=(
        "The system interface must spawn every subprocess written in this project through "
        "imbue.system_interface.subprocess_runner.run_detached_command, which puts the child in its "
        "own session. A child that inherits this service's controlling terminal can stop the whole "
        "service just by touching that terminal when it is killed (the kernel answers a read or a "
        "mode change from a background process group with SIGTTIN / SIGTTOU addressed to the whole "
        "group), which wedges the workspace: the socket keeps accepting and nothing answers. Do "
        "not call run_local_command_modern_version, subprocess.Popen/run, or os.system directly "
        "-- extend run_detached_command instead."
    ),
)

_BACKGROUND_SPAWN_RULE = RatchetRuleInfo(
    rule_name="ConcurrencyGroup process spawns outside agent_manager.py",
    rule_description=(
        "A long-running background process started through ConcurrencyGroup reaches the same "
        "subprocess runner as a direct call, so it inherits this service's controlling terminal "
        "unless it asks not to, and terminating it can then stop the whole service. Only "
        "agent_manager.py's mngr observe child spawns this way; it passes "
        "is_detached_from_terminal=True. A new one must do the same -- and prefer "
        "subprocess_runner.run_detached_command unless the process genuinely has to outlive the "
        "call."
    ),
)

# subprocess_runner.py is the boundary itself. The test files stand up their own children to
# exercise unrelated machinery (a git repo to discover, a server to talk to), outside the
# terminal shape these rules are about.
_TEST_FILES = ("*_test.py", "test_*.py", "conftest.py", "testing.py")
_ALLOWED_RAW_SPAWN_FILES = ("subprocess_runner.py", *_TEST_FILES)
_ALLOWED_BACKGROUND_SPAWN_FILES = ("agent_manager.py", *_TEST_FILES)


def test_prevent_subprocess_spawns_outside_the_detached_runner() -> None:
    pattern = RegexPattern(
        r"run_local_command_modern_version\(|subprocess\.(?:Popen|run|call|check_call|check_output)\(|os\.system\(",
        multiline=False,
    )
    chunks = check_regex_ratchet(_SOURCE, FileExtension(".py"), pattern, _ALLOWED_RAW_SPAWN_FILES)
    assert len(chunks) <= snapshot(0), _RAW_SPAWN_RULE.format_failure(chunks)


def test_prevent_background_process_spawns_outside_the_one_that_detaches_itself() -> None:
    pattern = RegexPattern(
        r"run_process_in_background\(|run_process_to_completion\(|run_background\(",
        multiline=False,
    )
    chunks = check_regex_ratchet(_SOURCE, FileExtension(".py"), pattern, _ALLOWED_BACKGROUND_SPAWN_FILES)
    assert len(chunks) <= snapshot(0), _BACKGROUND_SPAWN_RULE.format_failure(chunks)


def test_the_allowlisted_background_spawn_detaches_itself() -> None:
    """agent_manager.py is exempt from the rule above, so nothing else would notice it going
    back to an attached spawn -- which is how the observe child was missed to begin with."""
    source = (_SOURCE / "imbue" / "system_interface" / "agent_manager.py").read_text()
    assert source.count("run_process_in_background(") == 1, (
        "agent_manager.py gained another background spawn; the shared rule cannot see them, "
        "so each one has to be checked here"
    )
    assert "is_detached_from_terminal=True" in source
