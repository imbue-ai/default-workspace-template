"""Tests holding the spawn rules to what their descriptions promise.

A consuming app asserts its count is 0, which a rule that matches nothing passes exactly as
quietly as one that works -- so nothing in either app would notice a pattern that stopped
matching. These are the only tests that would.

The patterns are compiled here the way the ratchet machinery compiles them, from the rule's own
``pattern_string`` and ``is_multiline``, so what is searched is the real rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from imbue.imbue_common.ratchet_testing.common_ratchets import RegexRatchetRule
from imbue.imbue_common.ratchet_testing.core import RegexPattern

from detached_subprocess.ratchets import (
    BACKGROUND_SPAWN_NAMES,
    BACKGROUND_SPAWN_RULE,
    RAW_SPAWN_RULE,
    called_name,
    find_undetached_background_spawns,
)


def _matches(rule: RegexRatchetRule, source: str) -> bool:
    return RegexPattern(rule.pattern_string, multiline=rule.is_multiline).compiled.search(source) is not None


@pytest.mark.parametrize(
    "source",
    [
        "    result = run_local_command_modern_version(command=cmd, timeout=1.0)",
        "from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version",
        "from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version as _run",
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
    ],
)
def test_the_raw_spawn_rule_leaves_the_sanctioned_path_and_prose_alone(source: str) -> None:
    assert not _matches(RAW_SPAWN_RULE, source)


@pytest.mark.parametrize("name", BACKGROUND_SPAWN_NAMES)
def test_the_background_spawn_rule_catches_every_concurrency_group_entry_point(name: str) -> None:
    """Parameterized over the shared tuple, so a name dropped from it fails here rather than
    silently going unwatched in both apps."""
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
