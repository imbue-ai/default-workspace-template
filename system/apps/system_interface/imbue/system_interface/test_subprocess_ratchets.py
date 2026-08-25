"""Project-specific ratchet confining subprocess spawning to ``subprocess_runner.py``.

The system interface runs in a background process group on the workspace's tmux terminal, so
a child that inherits that terminal can stop the whole service when it is killed -- see
``subprocess_runner.py`` for the mechanism. Nothing here spawns a child that needs the
terminal, so the rule is absolute: every subprocess goes through ``run_detached_command``.
This is allowlist-by-file rather than a count, because a single new raw call reintroduces the
whole failure mode.

Lives outside ``test_ratchets.py`` because that file must define the same test set across
every project (enforced by ``test_meta_ratchets.py``).
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
        "The system interface must spawn every subprocess through "
        "imbue.system_interface.subprocess_runner.run_detached_command, which puts the child in its "
        "own session. A child that inherits this service's controlling terminal can stop the whole "
        "service by restoring terminal modes when it is killed (the kernel sends SIGTTOU to the "
        "background process group), which wedges the workspace: the socket keeps accepting and "
        "nothing answers. Do not call run_local_command_modern_version, subprocess.Popen/run, or "
        "os.system directly -- extend run_detached_command instead."
    ),
)

# subprocess_runner.py is the boundary itself. The test files stand up their own children to
# exercise unrelated machinery (a git repo to discover, a server to talk to), outside the
# terminal shape this rule is about.
_ALLOWED_FILES = (
    "subprocess_runner.py",
    "*_test.py",
    "test_*.py",
    "conftest.py",
    "testing.py",
)


def test_prevent_subprocess_spawns_outside_the_detached_runner() -> None:
    pattern = RegexPattern(
        r"run_local_command_modern_version\(|subprocess\.(?:Popen|run|call|check_call|check_output)\(|os\.system\(",
        multiline=False,
    )
    chunks = check_regex_ratchet(_SOURCE, FileExtension(".py"), pattern, _ALLOWED_FILES)
    assert len(chunks) <= snapshot(0), _RAW_SPAWN_RULE.format_failure(chunks)
