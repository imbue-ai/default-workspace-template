"""Tests holding the spawn rules to what their descriptions promise.

A consuming app asserts its count is 0, which a rule that matches nothing passes exactly as
quietly as one that works -- so nothing in either app would notice a pattern that stopped
matching, or a spawn entry point the rules stopped naming. These are the only tests that would.

The patterns are compiled here the way the ratchet machinery compiles them, from the rule's own
``pattern_string`` and ``is_multiline``, so what is searched is the real rule.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from imbue.concurrency_group import concurrency_group, local_process, subprocess_utils
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.ratchet_testing.common_ratchets import RegexRatchetRule
from imbue.imbue_common.ratchet_testing.core import RegexPattern

from detached_subprocess.ratchets import (
    BACKGROUND_SPAWN_NAMES,
    BACKGROUND_SPAWN_RULE,
    RAW_SPAWN_RULE,
    called_name,
    find_undetached_background_spawns,
)

# The raw runner takes the detachment flag too, but it is RAW_SPAWN_RULE that watches it, so it
# is the one entry point deliberately absent from BACKGROUND_SPAWN_NAMES.
_RAW_RUNNER_NAME = "run_local_command_modern_version"
# Where a spawn entry point can be defined: the two modules that spawn, and the class whose
# methods are the API a service is meant to start processes through.
_SPAWN_NAMESPACES = (concurrency_group, local_process, subprocess_utils, ConcurrencyGroup)


def _matches(rule: RegexRatchetRule, source: str) -> bool:
    return RegexPattern(rule.pattern_string, multiline=rule.is_multiline).compiled.search(source) is not None


def _spawn_entry_point_names() -> set[str]:
    """Every public concurrency_group callable that can put its child in its own session.

    Taking ``is_detached_from_terminal`` is what makes one: the flag exists only on the path
    down to the subprocess runner, so it names the reachable spawn entry points without this
    test having to keep a second list of them.
    """
    names: set[str] = set()
    for namespace in _SPAWN_NAMESPACES:
        for name, member in inspect.getmembers(namespace, callable):
            if name.startswith("_"):
                continue
            try:
                signature = inspect.signature(member)
            except (TypeError, ValueError):
                continue
            if "is_detached_from_terminal" in signature.parameters:
                names.add(name)
    return names


@pytest.mark.parametrize(
    "source",
    [
        "    result = run_local_command_modern_version(command=cmd, timeout=1.0)",
        "from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version",
        "from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version as _run",
        # The continuation line of a parenthesized import, which is how the name arrives when a
        # formatter is not holding every import to one line.
        "from imbue.concurrency_group.subprocess_utils import (\n    run_local_command_modern_version,\n)",
        "    process = subprocess.Popen(cmd)",
        "    subprocess.run(cmd, check=True)",
        "    subprocess.call(cmd)",
        "    subprocess.check_call(cmd)",
        "    output = subprocess.check_output(cmd)",
        '    os.system("ls")',
    ],
)
def test_the_raw_spawn_rule_catches_every_spelling_it_names(source: str) -> None:
    assert _matches(RAW_SPAWN_RULE, source)


@pytest.mark.parametrize(
    "source",
    [
        "    result = run_detached_command(command=cmd, timeout=1.0)",
        "from detached_subprocess.runner import run_detached_command",
        "import subprocess",
        "    except subprocess.CalledProcessError:",
        "    stdout=subprocess.PIPE,",
        "# the runner sends SIGTERM, which is what run_local_command_modern_version does on timeout",
        "    run_local_command_modern_version_wrapper,",
    ],
)
def test_the_raw_spawn_rule_leaves_the_sanctioned_path_and_prose_alone(source: str) -> None:
    assert not _matches(RAW_SPAWN_RULE, source)


def test_every_spawn_entry_point_that_can_detach_is_named_by_a_rule() -> None:
    """The rules are only as wide as the list they are built from, and nothing else measures it.

    ``BACKGROUND_SPAWN_RULE``'s pattern is joined out of ``BACKGROUND_SPAWN_NAMES``, so a name
    dropped from that tuple takes its own pattern alternative with it and every count still
    reads 0. Comparing against the real API is what notices -- in either direction: a dropped
    name, or a new concurrency_group spawn entry point that neither rule watches yet.
    """
    assert _spawn_entry_point_names() == {_RAW_RUNNER_NAME, *BACKGROUND_SPAWN_NAMES}, (
        "concurrency_group's spawn entry points and the names the rules watch have diverged; "
        "either BACKGROUND_SPAWN_NAMES lost one, or a new one needs adding to it"
    )


@pytest.mark.parametrize("name", BACKGROUND_SPAWN_NAMES)
def test_the_background_spawn_rule_catches_every_name_it_is_built_from(name: str) -> None:
    """That the joined pattern really matches a call to each name, which the count cannot show."""
    assert _matches(BACKGROUND_SPAWN_RULE, f"    handle = group.{name}(command=cmd)")


def test_the_background_spawn_rule_leaves_the_runner_alone() -> None:
    assert not _matches(BACKGROUND_SPAWN_RULE, "    result = run_detached_command(command=cmd)")


def _write_module(tmp_path: Path, body: str) -> Path:
    module_path = tmp_path / "spawner.py"
    module_path.write_text(body, encoding="utf-8")
    return module_path


def test_a_spawn_asking_to_detach_is_not_reported_as_undetached(tmp_path: Path) -> None:
    module_path = _write_module(
        tmp_path, "group.run_process_in_background(command=cmd, is_detached_from_terminal=True)\n"
    )

    spawns, undetached = find_undetached_background_spawns(module_path)

    assert [called_name(spawn) for spawn in spawns] == ["run_process_in_background"]
    assert undetached == []


@pytest.mark.parametrize(
    "spawn_source",
    [
        "group.run_process_in_background(command=cmd)",
        "group.run_process_in_background(command=cmd, is_detached_from_terminal=False)",
        # A variable rather than a literal: the caller's disposition is not readable here, so it
        # cannot stand as the guarantee the exemption is granted on.
        "group.run_process_in_background(command=cmd, is_detached_from_terminal=is_detached)",
    ],
)
def test_a_spawn_that_does_not_literally_ask_to_detach_is_reported(tmp_path: Path, spawn_source: str) -> None:
    module_path = _write_module(tmp_path, f"{spawn_source}\n")

    spawns, undetached = find_undetached_background_spawns(module_path)

    assert len(spawns) == 1
    assert undetached == spawns


def test_a_module_that_starts_no_background_process_reports_none(tmp_path: Path) -> None:
    module_path = _write_module(tmp_path, "result = run_detached_command(command=cmd)\n")

    assert find_undetached_background_spawns(module_path) == ([], [])
