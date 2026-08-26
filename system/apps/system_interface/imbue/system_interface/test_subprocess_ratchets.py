"""Project-specific ratchet confining subprocess spawning to ``subprocess_runner.py``.

The system interface runs in a background process group on the workspace's tmux terminal, so
a child that inherits that terminal can stop the whole service when it is killed -- see
``subprocess_runner.py`` for the mechanism. Nothing here spawns a child that needs the
terminal, so every subprocess must be detached from it. These are allowlist-by-file rather
than counts, because a single new attached spawn reintroduces the whole failure mode.

There are two rules because there are two ways to spawn. Run-to-completion commands go through
``run_detached_command``. Long-running background processes cannot -- they need
``ConcurrencyGroup`` -- so they ask it for the detachment themselves, and a rule looking only
for the raw runner call would not see them.

Lives outside ``test_ratchets.py`` because that file is the per-project standard set, and
these rules are specific to this project.
"""

import ast
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.common_ratchets import RatchetRuleInfo
from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RegexPattern
from imbue.imbue_common.ratchet_testing.core import check_regex_ratchet
from imbue.imbue_common.ratchet_testing.core import get_ast_nodes_of_type

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

# Every ConcurrencyGroup entry point that reaches the subprocess runner. Shared by the rule and
# by the guard standing behind agent_manager.py's exemption from it: the exemption waives all of
# these at once, so a guard that knew about fewer would leave the rest unchecked in that file.
_BACKGROUND_SPAWN_NAMES = ("run_process_in_background", "run_process_to_completion", "run_background")


def _called_name(call: ast.Call) -> str | None:
    """The bare name of what ``call`` invokes, for both ``f(...)`` and ``obj.f(...)``."""
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def test_prevent_subprocess_spawns_outside_the_detached_runner() -> None:
    pattern = RegexPattern(
        r"run_local_command_modern_version\(|subprocess\.(?:Popen|run|call|check_call|check_output)\(|os\.system\(",
        multiline=False,
    )
    chunks = check_regex_ratchet(_SOURCE, FileExtension(".py"), pattern, _ALLOWED_RAW_SPAWN_FILES)
    assert len(chunks) <= snapshot(0), _RAW_SPAWN_RULE.format_failure(chunks)


def test_prevent_background_process_spawns_outside_the_one_that_detaches_itself() -> None:
    pattern = RegexPattern("|".join(rf"{name}\(" for name in _BACKGROUND_SPAWN_NAMES), multiline=False)
    chunks = check_regex_ratchet(_SOURCE, FileExtension(".py"), pattern, _ALLOWED_BACKGROUND_SPAWN_FILES)
    assert len(chunks) <= snapshot(0), _BACKGROUND_SPAWN_RULE.format_failure(chunks)


def test_the_allowlisted_background_spawns_detach_themselves() -> None:
    """agent_manager.py is exempt from the rule above, so nothing else would notice a spawn there
    going back to attached."""
    agent_manager_path = _SOURCE / "imbue" / "system_interface" / "agent_manager.py"
    spawns = [
        call
        for call in get_ast_nodes_of_type(agent_manager_path, ast.Call)
        if _called_name(call) in _BACKGROUND_SPAWN_NAMES
    ]
    assert spawns, "agent_manager.py's background spawn is gone or renamed, so this guard checks nothing"
    for spawn in spawns:
        detachment = {keyword.arg: keyword.value for keyword in spawn.keywords}.get("is_detached_from_terminal")
        assert isinstance(detachment, ast.Constant) and detachment.value is True, (
            f"the {_called_name(spawn)} call on line {spawn.lineno} of agent_manager.py must pass "
            "is_detached_from_terminal=True; without it the child inherits this service's controlling "
            "terminal and can stop the whole service when killed"
        )
