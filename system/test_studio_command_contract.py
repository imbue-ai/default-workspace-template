"""Imbue Studio's commands in this workspace still answer the way the app runs them.

The Imbue Studio app runs scripts of this template inside the workspace (sending a chat a
message, seeding the welcome chat, collecting a bug report, ...). Which ones, with what
arguments, and what it reads back is the app's workspace command contract,
``apps/minds/imbue/minds/workspace_programs/workspace_command_contract.py`` in mngr, published
beside it as ``workspace_command_contract.json``, which ``system/scripts/fetch_mngr_assets.sh``
vendors here from the commit ``pyproject.toml`` pins.
These checks run every declared command for real against this checkout. They live directly in
``system/``, so every change runs them (``app-manifest select-tests`` always selects
``system/test_*.py``).

A failure here means a change in this workspace broke the app's way in, not that the contract
is wrong: put the command back the way the contract says. A change to the contract itself is
made in mngr and reaches this template through its pin; see
``apps/minds/docs/workspace-command-contract.md`` there.

The vendor directory is fetched by the ``system/`` npm pre-build and pre-test hooks, which CI
runs before pytest, and by every workspace build; a local run that has never built the frontend
may not have it, so an absent vendor directory skips rather than fails.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from entry_points_testing import REPO_ROOT, SYSTEM_SCRIPTS

_VENDOR_ASSETS_DIR = REPO_ROOT / "system" / "vendor" / "mngr-assets"
_CONTRACT_PATH = (
    _VENDOR_ASSETS_DIR
    / "apps"
    / "minds"
    / "imbue"
    / "minds"
    / "workspace_programs"
    / "workspace_command_contract.json"
)

# The contract version these scripts were last reviewed against. A new version changes what an
# existing command must do (an exit status's meaning, an output's shape), which the structural
# checks below cannot see: re-check the scripts against the contract, then move this.
_REVIEWED_CONTRACT_VERSION = 1

# Commands that parse their own arguments instead of through argparse, so ``--help`` lists
# nothing: each maps to the module whose string literals must carry every option the app passes.
_HAND_PARSED_COMMANDS = {
    "collect_bug_report_diagnostics": SYSTEM_SCRIPTS
    / "workspace_bare_scripts"
    / "collect_bug_report_diagnostics.py",
}

_UNKNOWN_OPTION = "--studio-contract-probe-unknown-option"
_COMMAND_TIMEOUT_SECONDS = 60
_SYSTEM_PYTHON = "system_python"
_ROOT_VENV_ENTRY_POINT = "root_venv_entry_point"


def _broken_contract_message(command: dict[str, Any], problem: str) -> str:
    return (
        f"Imbue Studio runs `{command['target']}` ({command['name']}) inside this workspace, and "
        f"{problem}. Its interface is the app's workspace command contract, vendored at "
        f"{_CONTRACT_PATH.relative_to(REPO_ROOT)}: put the command back the way that contract "
        "declares it rather than changing these checks; a change to the interface is made "
        "in mngr (apps/minds/docs/workspace-command-contract.md)."
    )


def _load_contract() -> dict[str, Any]:
    if not _VENDOR_ASSETS_DIR.is_dir():
        pytest.skip(
            "mngr assets are not vendored here; run system/scripts/fetch_mngr_assets.sh first"
        )
    # The directory exists, so the fetch ran: a missing contract means the pinned mngr commit
    # does not publish it (or the asset list lost it), which is a real break.
    assert _CONTRACT_PATH.is_file(), (
        f"{_CONTRACT_PATH.relative_to(REPO_ROOT)} is missing from the vendored mngr assets"
    )
    return json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))


def _command_named(contract: dict[str, Any], name: str) -> dict[str, Any]:
    (command,) = [c for c in contract["commands"] if c["name"] == name]
    return command


def _command_prefix(command: dict[str, Any]) -> list[str]:
    """How the app starts ``command``: the system python with no site-packages, or the root venv's script."""
    if command["runner"] == _SYSTEM_PYTHON:
        return [sys.executable, "-S", "-s", str(REPO_ROOT / command["target"])]
    if command["runner"] == _ROOT_VENV_ENTRY_POINT:
        return [str(Path(sys.executable).parent / command["target"])]
    raise AssertionError(f"unknown runner {command['runner']!r} for {command['name']}")


def _environment(command: dict[str, Any]) -> dict[str, str]:
    if command["runner"] == _SYSTEM_PYTHON:
        return {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    return dict(os.environ)


def _run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=_COMMAND_TIMEOUT_SECONDS,
        check=False,
    )


def _mentions(text: str, word: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text) is not None


def _argparse_choices(help_text: str) -> set[str]:
    """Every value of every argparse choice set (``{a,b,...}``) the help text shows."""
    return {
        value
        for group in re.findall(r"\{([^{}\s]+)\}", help_text)
        for value in group.split(",")
    }


def _string_literals(path: Path) -> set[str]:
    return {
        node.value
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _argparse_problems(command: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for invocation in command["invocations"]:
        result = _run(
            [*_command_prefix(command), *invocation["subcommand"], "--help"],
            _environment(command),
        )
        shown = " ".join([command["target"], *invocation["subcommand"], "--help"])
        if result.returncode != 0:
            problems.append(
                f"`{shown}` exited {result.returncode}: {result.stderr.strip()[-500:]}"
            )
            continue
        options = (*invocation["value_options"], *invocation["flag_options"])
        missing = [word for word in options if not _mentions(result.stdout, word)]
        if missing:
            problems.append(
                f"`{shown}` no longer lists {missing}, which the app passes to "
                f"{invocation['purpose']}"
            )
        choices = _argparse_choices(result.stdout)
        missing_choices = [
            value for value in invocation["fixed_values"] if value not in choices
        ]
        if missing_choices:
            problems.append(
                f"`{shown}` no longer offers {missing_choices} as a choice, which the app "
                f"passes to {invocation['purpose']}"
            )
    return problems


def _hand_parsed_problems(command: dict[str, Any], module_path: Path) -> list[str]:
    literals = _string_literals(module_path)
    problems: list[str] = []
    for invocation in command["invocations"]:
        for option in invocation["flag_options"]:
            if option not in literals:
                problems.append(
                    f"{module_path.name} no longer reads {option!r}, which the app passes to "
                    f"{invocation['purpose']}"
                )
        for option in invocation["value_options"]:
            if f"{option}=" not in literals:
                problems.append(
                    f"{module_path.name} no longer reads {option!r}=<value>, which the app passes "
                    f"to {invocation['purpose']}"
                )
    return problems


def test_the_contract_is_the_version_these_scripts_were_reviewed_against() -> None:
    contract = _load_contract()

    assert contract["contract_version"] == _REVIEWED_CONTRACT_VERSION, (
        f"the pinned Imbue Studio app declares workspace command contract version "
        f"{contract['contract_version']}, and these scripts were last reviewed against "
        f"{_REVIEWED_CONTRACT_VERSION}: check every command against the new version, then "
        "move _REVIEWED_CONTRACT_VERSION"
    )


def test_every_hand_parsed_command_is_one_the_contract_declares() -> None:
    contract = _load_contract()
    names = {command["name"] for command in contract["commands"]}

    assert set(_HAND_PARSED_COMMANDS) <= names


@pytest.mark.timeout(60)
def test_every_declared_command_takes_every_argument_the_app_passes() -> None:
    contract = _load_contract()
    failures: list[str] = []
    for command in contract["commands"]:
        hand_parsed_module = _HAND_PARSED_COMMANDS.get(command["name"])
        problems = (
            _argparse_problems(command)
            if hand_parsed_module is None
            else _hand_parsed_problems(command, hand_parsed_module)
        )
        failures.extend(
            _broken_contract_message(command, problem) for problem in problems
        )

    assert not failures, "\n\n".join(failures)


@pytest.mark.timeout(60)
def test_the_bug_report_collector_runs_with_no_venv_and_prints_nothing_when_asked_for_nothing() -> (
    None
):
    contract = _load_contract()
    command = _command_named(contract, "collect_bug_report_diagnostics")
    result = _run(
        [*_command_prefix(command), "--scan-timeout=1"],
        _environment(command),
    )

    assert result.returncode == 0, _broken_contract_message(
        command, f"it exited {result.returncode}: {result.stderr.strip()[-500:]}"
    )
    assert result.stdout == "", _broken_contract_message(
        command, f"it printed {result.stdout[:200]!r} when nothing was requested"
    )


@pytest.mark.timeout(60)
def test_an_unknown_argument_exits_with_the_status_the_app_reads_as_an_older_template() -> (
    None
):
    contract = _load_contract()
    failures: list[str] = []
    for command in contract["commands"]:
        expected = command["usage_error_exit_code"]
        if expected is None:
            continue
        result = _run(
            [*_command_prefix(command), _UNKNOWN_OPTION],
            _environment(command),
        )
        if result.returncode != expected:
            failures.append(
                _broken_contract_message(
                    command,
                    f"an argument it does not know exits {result.returncode}, not the "
                    f"{expected} the app reads as a template from before that argument",
                )
            )

    assert not failures, "\n\n".join(failures)


def test_every_path_the_app_checks_for_is_here() -> None:
    contract = _load_contract()
    missing = [
        f"`{required['path']}`: {required['reason']}"
        for required in contract["required_paths"]
        if not (REPO_ROOT / required["path"]).exists()
    ]

    assert not missing, (
        "Imbue Studio checks for these template paths, and they are gone (its workspace "
        f"command contract, vendored at {_CONTRACT_PATH.relative_to(REPO_ROOT)}, lists them):\n"
        + "\n".join(missing)
    )
