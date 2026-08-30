"""Guard: the update-self flow asks of ``create_worker.py`` only what old launchers provide.

The update-self SKILL runs cross-version: once the target is resolved, the
lead follows the *target* release's copy of the prose (SKILL.md 2a) but
launches with the workspace's *own* ``launch-task/create_worker.py``, which
is whatever release the workspace is still on. A flag the prose reaches for
that the local launcher predates fails the launch on exactly the workspaces
the update exists for. Staging a newer launcher alongside would not help: it
may itself depend on settings or plugins the merge has not landed yet.

So every ``create_worker.py`` invocation in the update-self prose is held to
the launcher interface of the oldest release the Minds app offers an update
from (``minds-v0.3.17``). Adding a flag here means checking that release's
``create_worker.py`` still accepts it, or doing the work with plain ``mngr``
commands instead, as the predecessor cleanup in Step 3b does.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parents[1]
_PROSE_FILES = (
    _SKILL_DIR / "SKILL.md",
    _SKILL_DIR / "references" / "update-self-worker.md",
)

# The launcher's interface at minds-v0.3.17, per subcommand.
_LAUNCHER_FLAGS_AT_FLOOR: dict[str, frozenset[str]] = {
    "launch": frozenset({"--name", "--template", "--runtime-dir", "--task-file"}),
    "await": frozenset({"--name", "--task-file", "--timeout", "--poll-interval"}),
    "launch-sync": frozenset(
        {
            "--name",
            "--template",
            "--runtime-dir",
            "--task-file",
            "--timeout",
            "--poll-interval",
            "--keep-agent",
            "--result-json",
        }
    ),
    "destroy": frozenset({"--name"}),
}

_FENCED_CODE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def _launcher_invocations(text: str) -> list[list[str]]:
    """Every ``create_worker.py <subcommand> ...`` argv in the fenced code blocks."""
    invocations: list[list[str]] = []
    for block in _FENCED_CODE.findall(text):
        # A block may hold several commands; each launcher call is one
        # (possibly line-continued) command.
        for command in re.split(r"\n(?=\S)", block.replace("\\\n", " ")):
            if "create_worker.py" not in command:
                continue
            words = shlex.split(command, comments=True)
            for index, word in enumerate(words):
                if word.endswith("create_worker.py") and index + 1 < len(words):
                    invocations.append(words[index + 1 :])
                    break
    return invocations


def test_update_self_prose_uses_only_the_floor_launcher_interface() -> None:
    invocations = [
        (prose.name, argv)
        for prose in _PROSE_FILES
        for argv in _launcher_invocations(prose.read_text())
    ]
    # The flow does launch and await through the launcher; a rewrite that
    # stopped doing so would silently make this guard vacuous.
    assert {argv[0] for _, argv in invocations} >= {"launch", "await"}

    for prose_name, argv in invocations:
        subcommand = argv[0]
        assert subcommand in _LAUNCHER_FLAGS_AT_FLOOR, (
            f"{prose_name}: create_worker.py {subcommand} is not a subcommand of "
            "the floor launcher"
        )
        flags = {word.split("=", 1)[0] for word in argv[1:] if word.startswith("--")}
        unsupported = sorted(flags - _LAUNCHER_FLAGS_AT_FLOOR[subcommand])
        assert not unsupported, (
            f"{prose_name}: create_worker.py {subcommand} is given {unsupported}, "
            "which the launcher at minds-v0.3.17 does not accept; the prose runs "
            "against the workspace's own launcher, so do this with plain mngr "
            "commands instead"
        )
