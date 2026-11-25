"""Unit tests for the stopped-agent upgrade the boot and update-self share."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from loguru import logger
from mngr_cli_contract.contract import assert_mngr_argv_valid

from bootstrap.agent_upgrade import (
    _MNGR_LIST_TIMEOUT_SECONDS,
    _MNGR_UPGRADE_TIMEOUT_SECONDS,
    COVERAGE_RECORD,
    MNGR_LIST_AGENT_STATES_ARGV,
    WORKSPACE_VENV,
    installed_mngr,
    upgrade_stopped_agents,
)
from bootstrap.testing import (
    MNGR_GIT_URL,
    StubSubprocess,
    install_mngr,
    install_package,
    mngr_upgrade_outcome,
)

_VENV_MNGR = str(WORKSPACE_VENV / "bin" / "mngr")
_LIST_ARGV = [_VENV_MNGR, *MNGR_LIST_AGENT_STATES_ARGV]


def _upgrade_argv(*agent_ids: str) -> list[str]:
    return [
        _VENV_MNGR,
        "upgrade",
        *agent_ids,
        "--on-error",
        "continue",
        "--format",
        "jsonl",
    ]


def _upgraded(agent_id: str) -> str:
    return mngr_upgrade_outcome(
        "agent_upgraded", agent_id, plan="LEAVE_STOPPED", prior_state="STOPPED"
    )


def _skipped(agent_id: str, kind: str, detail: str) -> str:
    return mngr_upgrade_outcome("agent_skipped", agent_id, kind=kind, detail=detail)


def _stub_in_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, listing: str
) -> StubSubprocess:
    """A stubbed ``subprocess.run`` whose ``mngr list`` prints ``listing``, from a repo root at ``tmp_path``."""
    monkeypatch.chdir(tmp_path)
    stub = StubSubprocess()
    stub.reply_on = {"list": (0, listing)}
    monkeypatch.setattr("bootstrap.agent_upgrade.subprocess.run", stub.run)
    return stub


@contextmanager
def _captured_log_lines(level: str) -> Generator[list[str], None, None]:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level=level)
    try:
        yield lines
    finally:
        logger.remove(sink)


def _write_fake_venv_mngr(tmp_path: Path, body: str) -> Path:
    """A shell script at the workspace venv's mngr path under ``tmp_path``; returns the file each call appends to."""
    calls = tmp_path / "calls"
    double = tmp_path / WORKSPACE_VENV / "bin" / "mngr"
    double.parent.mkdir(parents=True)
    double.write_text(f'#!/bin/sh\necho "$*" >> {calls}\n{body}')
    double.chmod(0o755)
    return calls


# which agents are named


def test_the_upgrade_names_only_the_stopped_agents_to_the_venv_mngr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A running agent is never named: `mngr upgrade` would restart an idle one behind the chat app.
    stub = _stub_in_workspace(
        monkeypatch,
        tmp_path,
        "agent-a|STOPPED\nagent-b|WAITING\nagent-c|STOPPED\nagent-d|RUNNING\n"
        "agent-e|RUNNING_BACKGROUND\nagent-f|UNKNOWN\nnot a row\n",
    )

    upgrade_stopped_agents()

    assert stub.calls == [_LIST_ARGV, _upgrade_argv("agent-a", "agent-c")]
    for argv in stub.calls:
        assert_mngr_argv_valid(argv)
    for kwargs in stub.kwargs:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["MNGR_ALLOW_UNKNOWN_CONFIG"] == "1"
    assert [kwargs["timeout"] for kwargs in stub.kwargs] == [
        _MNGR_LIST_TIMEOUT_SECONDS,
        _MNGR_UPGRADE_TIMEOUT_SECONDS,
    ]


def test_the_upgrade_runs_the_workspace_venvs_mngr_from_the_repo_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The boot converges the venv to the tree's pin; an mngr on PATH may lag it.
    monkeypatch.chdir(tmp_path)
    calls = tmp_path / "calls"
    for bin_dir, label in (
        (tmp_path / ".venv" / "bin", "venv"),
        (tmp_path / "tool-bin", "path"),
    ):
        bin_dir.mkdir(parents=True)
        double = bin_dir / "mngr"
        double.write_text(
            "#!/bin/sh\n"
            f'echo "{label} $1 $MNGR_ALLOW_UNKNOWN_CONFIG" >> {calls}\n'
            'if [ "$1" = list ]; then echo "agent-a|STOPPED"; fi\n'
        )
        double.chmod(0o755)
    monkeypatch.setenv(
        "PATH", f"{tmp_path / 'tool-bin'}{os.pathsep}{os.environ['PATH']}"
    )

    upgrade_stopped_agents()

    assert calls.read_text().splitlines() == ["venv list 1", "venv upgrade 1"]


def test_the_upgrade_makes_no_call_when_no_agent_is_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A fresh create or a pool adopt: only the running services agent exists.
    stub = _stub_in_workspace(
        monkeypatch, tmp_path, "agent-services|RUNNING\nagent-b|WAITING\n"
    )

    upgrade_stopped_agents()

    assert stub.calls == [_LIST_ARGV]


def test_the_upgrade_takes_the_stopped_agents_a_failing_listing_still_listed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With --on-error continue, one agent that cannot be read does not hide the others.
    stub = _stub_in_workspace(monkeypatch, tmp_path, "")
    stub.reply_on = {"list": (1, "agent-a|STOPPED\n")}

    upgrade_stopped_agents()

    assert stub.calls[1:] == [_upgrade_argv("agent-a")]


# what reaches the log


def test_mngr_output_that_is_not_utf_8_reaches_the_log_without_stopping_the_upgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # mngr can echo a path whose bytes are not UTF-8, such as an agent's file in a failure line.
    monkeypatch.chdir(tmp_path)
    reply = tmp_path / "upgrade-reply"
    reply.write_bytes(
        mngr_upgrade_outcome(
            "agent_failed",
            "agent-a",
            step="PREPARE",
            error_message="cannot read /tmp/@",
        )
        .encode()
        .replace(b"@", b"\xff")
    )
    calls = _write_fake_venv_mngr(
        tmp_path,
        'if [ "$1" = list ]; then printf "warning: \\377\\nagent-a|STOPPED\\n"; exit 0; fi\n'
        f"cat {reply}\nexit 1\n",
    )

    with _captured_log_lines("INFO") as lines:
        upgrade_stopped_agents()

    assert calls.read_text().splitlines() == [
        " ".join(MNGR_LIST_AGENT_STATES_ARGV),
        "upgrade agent-a --on-error continue --format jsonl",
    ]
    assert any(
        "mngr upgrade: failed a: prepare (cannot read /tmp/�)" in line for line in lines
    ), lines


def test_the_upgrade_logs_each_outcome_without_the_recreate_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Re-creating an agent restarts it, so the log never offers it.
    stub = _stub_in_workspace(monkeypatch, tmp_path, "")
    stub.reply_on = {
        "list": (0, "agent-a|STOPPED\nagent-b|STOPPED\n"),
        "upgrade": (
            9,
            _upgraded("agent-a")
            + _skipped(
                "agent-b",
                "UNSUPPORTED",
                "created before mngr kept its hooks in a file of its own; "
                "first remove the hooks mngr left in .claude/settings.local.json in its work dir; "
                "re-create it with `mngr create b --reuse --update`",
            )
            + json.dumps(
                {
                    "event": "upgrade_result",
                    "upgraded_count": 1,
                    "current_count": 0,
                    "skipped_count": 1,
                    "failed_count": 0,
                    "exit_code": 9,
                }
            )
            + "\n",
        ),
    }

    with _captured_log_lines("INFO") as lines:
        upgrade_stopped_agents()

    assert any("mngr upgrade: upgraded a (stopped)" in line for line in lines)
    assert any(
        line.rstrip().endswith(
            "mngr upgrade: skipped b: created before mngr kept its hooks in a file of its own; "
            "first remove the hooks mngr left in .claude/settings.local.json in its work dir"
        )
        for line in lines
    )
    assert any(
        "mngr upgrade: 1 upgraded, 0 current, 1 not upgraded, 0 failed" in line
        for line in lines
    )
    assert not any("--reuse" in line or "re-create" in line for line in lines)
    assert not any("WARNING" in line for line in lines)


@pytest.mark.parametrize(
    ("detail", "logged"),
    [
        ("it is mid-turn (--force interrupts it)", "it is mid-turn"),
        (
            "another program is running in its terminal (--force ends it and restarts the agent)",
            "another program is running in its terminal",
        ),
        (
            "its host is stopped (`mngr start a@box.docker` starts it)",
            "its host is stopped",
        ),
    ],
    ids=["busy", "replaced", "host-stopped"],
)
def test_a_skip_is_logged_without_the_command_mngr_suggests_for_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, detail: str, logged: str
) -> None:
    # Forcing an upgrade restarts a busy chat through mngr, so the outcome lines never offer it.
    stub = _stub_in_workspace(monkeypatch, tmp_path, "agent-a|STOPPED\n")
    stub.reply_on["upgrade"] = (9, _skipped("agent-a", "NOT_IDLE", detail))

    with _captured_log_lines("INFO") as lines:
        upgrade_stopped_agents()

    assert any(
        line.rstrip().endswith(f"mngr upgrade: skipped a: {logged}") for line in lines
    ), lines
    assert not any("--force" in line or "mngr start" in line for line in lines)


@pytest.mark.parametrize("returncode", [1, 8])
def test_an_upgrade_that_fails_is_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int
) -> None:
    stub = _stub_in_workspace(monkeypatch, tmp_path, "agent-a|STOPPED\n")
    stub.stderr = "Error: no agent matched agent-a"
    stub.reply_on["upgrade"] = (returncode, "")

    with _captured_log_lines("WARNING") as warnings:
        upgrade_stopped_agents()

    assert any(
        f"mngr upgrade exited {returncode}: Error: no agent matched agent-a" in line
        for line in warnings
    )


def test_an_agent_the_upgrade_failed_is_a_warning_that_names_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _stub_in_workspace(
        monkeypatch, tmp_path, "agent-a|STOPPED\nagent-b|STOPPED\n"
    )
    stub.reply_on["upgrade"] = (
        1,
        _upgraded("agent-a")
        + mngr_upgrade_outcome(
            "agent_failed",
            "agent-b",
            step="PREPARE",
            error_message="rsync failed: Not a directory",
        ),
    )

    with _captured_log_lines("WARNING") as warnings:
        upgrade_stopped_agents()

    assert any(
        "mngr upgrade: failed b: prepare (rsync failed: Not a directory)" in line
        for line in warnings
    )
    assert any(line.rstrip().endswith("mngr upgrade exited 1") for line in warnings)
    assert not any("upgraded a" in line for line in warnings)


@pytest.mark.parametrize(
    "raise_on",
    [
        {"list": FileNotFoundError(_VENV_MNGR)},
        {"list": subprocess.TimeoutExpired(_VENV_MNGR, 1.0)},
        {"upgrade": subprocess.TimeoutExpired(_VENV_MNGR, 1.0)},
    ],
    ids=["missing-mngr", "listing-timed-out", "upgrade-timed-out"],
)
def test_an_upgrade_that_cannot_run_is_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raise_on: dict[str, BaseException],
) -> None:
    stub = _stub_in_workspace(monkeypatch, tmp_path, "agent-a|STOPPED\n")
    stub.raise_on = raise_on

    with _captured_log_lines("WARNING") as warnings:
        upgrade_stopped_agents()

    (subcommand,) = raise_on
    assert any(f"mngr {subcommand} could not run" in line for line in warnings)


def test_an_upgrade_that_times_out_still_logs_the_outcomes_it_printed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _stub_in_workspace(
        monkeypatch, tmp_path, "agent-a|STOPPED\nagent-b|STOPPED\n"
    )
    stub.raise_on = {
        "upgrade": subprocess.TimeoutExpired(
            _VENV_MNGR,
            1.0,
            output=(
                _upgraded("agent-a")
                + mngr_upgrade_outcome(
                    "agent_failed", "agent-b", step="SWAP", error_message="disk full"
                )
            ).encode(),
        )
    }

    with _captured_log_lines("INFO") as lines:
        upgrade_stopped_agents()

    assert any(
        "INFO" in line and "mngr upgrade: upgraded a (stopped)" in line
        for line in lines
    )
    assert any(
        "WARNING" in line and "mngr upgrade: failed b: swap (disk full)" in line
        for line in lines
    )
    assert any("mngr upgrade could not run" in line for line in lines)


# the coverage record


def test_installed_mngr_is_the_commit_each_of_mngr_and_its_plugins_came_from(
    tmp_path: Path,
) -> None:
    expected = install_mngr(tmp_path / "venv", "c0ffee")

    assert installed_mngr(tmp_path / "venv") == expected
    assert set(expected) == {"imbue-mngr", "imbue-mngr-claude"}


@pytest.mark.parametrize(
    "direct_url",
    [
        None,
        {"url": "file:///src/mngr_codex", "dir_info": {"editable": True}},
        {"url": MNGR_GIT_URL, "vcs_info": {"vcs": "git"}},
    ],
    ids=["from-an-index", "from-a-directory", "no-commit"],
)
def test_installed_mngr_is_unknown_when_a_plugin_came_from_no_commit(
    tmp_path: Path, direct_url: dict[str, object] | None
) -> None:
    # A directory or an unpinned install can change its files under the same metadata.
    install_mngr(tmp_path / "venv", "c0ffee")
    install_package(
        tmp_path / "venv",
        "imbue-mngr-codex",
        "[mngr]\ncodex = imbue.mngr_codex.plugin\n",
        direct_url,
    )

    assert installed_mngr(tmp_path / "venv") is None


def test_installed_mngr_is_unknown_for_a_venv_without_mngr(tmp_path: Path) -> None:
    install_package(tmp_path / "venv", "loguru", "", None)

    assert installed_mngr(tmp_path / "venv") is None
    assert installed_mngr(tmp_path / "missing") is None


def _record(mngr: object) -> str:
    return json.dumps({"mngr": mngr})


@pytest.mark.parametrize(
    ("listing", "upgrade_reply"),
    [
        ("agent-services|RUNNING\nagent-a|STOPPED\n", (0, _upgraded("agent-a"))),
        (
            "agent-services|RUNNING\nagent-a|STOPPED\n",
            (0, mngr_upgrade_outcome("agent_current", "agent-a")),
        ),
        (
            "agent-services|RUNNING\nagent-a|STOPPED\n",
            (
                9,
                _skipped(
                    "agent-a", "UNSUPPORTED", "agent type 'pi' has no upgrade support"
                ),
            ),
        ),
        ("agent-services|RUNNING\n", None),
    ],
    ids=["upgraded", "current", "unsupported", "nothing-stopped"],
)
def test_a_pass_that_leaves_every_agent_current_records_the_venvs_mngr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    listing: str,
    upgrade_reply: tuple[int, str] | None,
) -> None:
    # The caller is the agent running the pass, which never reads stopped.
    stub = _stub_in_workspace(monkeypatch, tmp_path, listing)
    if upgrade_reply is not None:
        stub.reply_on["upgrade"] = upgrade_reply
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-services")
    installed = install_mngr(WORKSPACE_VENV, "c0ffee")

    upgrade_stopped_agents()

    assert json.loads(COVERAGE_RECORD.read_text()) == {"mngr": installed}


@pytest.mark.parametrize(
    ("listing", "upgrade_reply", "raise_on"),
    [
        ("agent-a|STOPPED\nagent-b|WAITING\n", (0, ""), {}),
        ("agent-a|STOPPED\nagent-b|UNKNOWN\n", (0, ""), {}),
        (
            "agent-a|STOPPED\n",
            (
                1,
                mngr_upgrade_outcome(
                    "agent_failed", "agent-a", step="SWAP", error_message="disk full"
                ),
            ),
            {},
        ),
        (
            "agent-a|STOPPED\n",
            (9, _skipped("agent-a", "NOT_IDLE", "its state could not be read")),
            {},
        ),
        (
            "agent-a|STOPPED\n",
            (
                9,
                _skipped(
                    "agent-a", "NOT_IDLE", "it is mid-turn (--force interrupts it)"
                ),
            ),
            {},
        ),
        ("agent-a|STOPPED\nagent-b|STOPPED\n", (0, _upgraded("agent-a")), {}),
        (
            "agent-a|STOPPED\n",
            (0, ""),
            {"upgrade": subprocess.TimeoutExpired(_VENV_MNGR, 1.0)},
        ),
        ("agent-a|STOPPED\n", (0, ""), {"list": FileNotFoundError(_VENV_MNGR)}),
        (
            "agent-a|STOPPED\n",
            (0, ""),
            {"list": subprocess.TimeoutExpired(_VENV_MNGR, 1.0)},
        ),
    ],
    ids=[
        "another-agent-running",
        "another-agent-unreadable",
        "an-agent-failed",
        "skipped-as-unreadable",
        "skipped-as-busy",
        "an-agent-unreported",
        "upgrade-timed-out",
        "mngr-missing",
        "listing-timed-out",
    ],
)
def test_a_pass_that_leaves_an_agent_behind_writes_no_record_and_removes_another_mngrs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    listing: str,
    upgrade_reply: tuple[int, str],
    raise_on: dict[str, BaseException],
) -> None:
    stub = _stub_in_workspace(monkeypatch, tmp_path, listing)
    stub.reply_on["upgrade"] = upgrade_reply
    stub.raise_on = raise_on
    install_mngr(WORKSPACE_VENV, "c0ffee")
    COVERAGE_RECORD.parent.mkdir(parents=True)
    COVERAGE_RECORD.write_text(_record(install_mngr(tmp_path / "old-venv", "0ld")))

    upgrade_stopped_agents()

    assert not COVERAGE_RECORD.exists()


def test_a_listing_that_exits_non_zero_writes_no_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An agent the listing could not read may be running, or may be stale.
    stub = _stub_in_workspace(monkeypatch, tmp_path, "")
    stub.reply_on["list"] = (1, "agent-a|STOPPED\n")
    install_mngr(WORKSPACE_VENV, "c0ffee")

    upgrade_stopped_agents()

    assert stub.calls[1:] == [_upgrade_argv("agent-a")]
    assert not COVERAGE_RECORD.exists()


def test_another_mngrs_record_is_gone_before_any_agent_is_touched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Once this mngr rewrites one agent, a record naming another no longer holds, whatever the pass completes.
    stub = _stub_in_workspace(monkeypatch, tmp_path, "agent-a|STOPPED\n")
    install_mngr(WORKSPACE_VENV, "c0ffee")
    COVERAGE_RECORD.parent.mkdir(parents=True)
    COVERAGE_RECORD.write_text(_record(install_mngr(tmp_path / "old-venv", "0ld")))
    record_present_at: dict[str, bool] = {}
    stub.on_command = lambda argv: record_present_at.update(
        {argv[1]: COVERAGE_RECORD.exists()}
    )

    upgrade_stopped_agents()

    assert record_present_at == {"list": False, "upgrade": False}


def test_a_record_naming_the_venvs_mngr_runs_no_mngr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _stub_in_workspace(monkeypatch, tmp_path, "agent-a|STOPPED\n")
    installed = install_mngr(WORKSPACE_VENV, "c0ffee")
    COVERAGE_RECORD.parent.mkdir(parents=True)
    COVERAGE_RECORD.write_text(_record(installed))

    with _captured_log_lines("INFO") as lines:
        upgrade_stopped_agents()

    assert stub.calls == []
    assert any(
        "Every agent is already current with this mngr" in line for line in lines
    )
    assert json.loads(COVERAGE_RECORD.read_text()) == {"mngr": installed}


def test_a_venv_whose_mngr_is_unknown_is_upgraded_with_no_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = _stub_in_workspace(monkeypatch, tmp_path, "agent-a|STOPPED\n")
    install_package(
        WORKSPACE_VENV,
        "imbue-mngr",
        "[console_scripts]\nmngr = imbue.mngr.main:cli\n",
        {"url": "file:///src/mngr", "dir_info": {"editable": True}},
    )
    COVERAGE_RECORD.parent.mkdir(parents=True)
    COVERAGE_RECORD.write_text(_record(None))

    upgrade_stopped_agents()

    assert stub.calls == [_LIST_ARGV, _upgrade_argv("agent-a")]
    assert not COVERAGE_RECORD.exists()


# the command update-self runs


def test_the_command_runs_the_pass_from_the_repo_root_and_says_each_outcome_on_stderr(
    tmp_path: Path,
) -> None:
    command = Path(sys.executable).parent / "upgrade-stopped-agents"
    _write_fake_venv_mngr(
        tmp_path,
        'if [ "$1" = list ]; then echo "agent-a|STOPPED"; exit 0; fi\n'
        f"echo '{_upgraded('agent-a').strip()}'\n",
    )
    installed = install_mngr(tmp_path / WORKSPACE_VENV, "c0ffee")

    result = subprocess.run(
        [str(command)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "mngr upgrade: upgraded a (stopped)" in result.stderr.splitlines()
    assert json.loads((tmp_path / COVERAGE_RECORD).read_text()) == {"mngr": installed}


def test_the_command_asked_for_help_prints_it_and_runs_no_mngr(tmp_path: Path) -> None:
    command = Path(sys.executable).parent / "upgrade-stopped-agents"
    calls = _write_fake_venv_mngr(tmp_path, 'echo "agent-a|STOPPED"\n')

    result = subprocess.run(
        [str(command), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: upgrade-stopped-agents" in result.stdout
    assert not calls.exists()
