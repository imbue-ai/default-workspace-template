import asyncio
import json
import time
from pathlib import Path

import pytest
from harbor.environments.base import ExecResult
from pydantic import SecretStr

from imbue.minds_evals import evidence_collection
from imbue.minds_evals import ui_flows
from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import CheckClass
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import Expectations
from imbue.minds_evals.data_types import HttpCheck
from imbue.minds_evals.data_types import ManifestEntry
from imbue.minds_evals.data_types import RegisteredApp
from imbue.minds_evals.expectations import expand_expectations
from imbue.minds_evals.expectations import parse_expectations
from imbue.minds_evals.mock_environment_test import MockBoxEnvironment
from imbue.minds_evals.mock_environment_test import ScriptedExecRule
from imbue.minds_evals.mock_environment_test import failed_result
from imbue.minds_evals.mock_environment_test import mngr_exec_json
from imbue.minds_evals.mock_environment_test import ok_result
from imbue.minds_evals.mock_verification_agent_test import ScriptedVerificationAgent
from imbue.minds_evals.mock_verification_agent_test import click_action
from imbue.minds_evals.mock_verification_agent_test import done_action
from imbue.minds_evals.mock_verification_agent_test import reading
from imbue.minds_evals.testing import FAKE_WORKSPACE_AGENT_ID
from imbue.minds_evals.testing import SCRIPT_REGISTERED_APPS
from imbue.minds_evals.testing import TEMPLATE_CONFIG_REGISTRATIONS
from imbue.minds_evals.testing import TEMPLATE_PREEXISTING_APPS
from imbue.minds_evals.testing import TEMPLATE_SUPERVISORD_CONF
from imbue.minds_evals.testing import probe_sections
from imbue.minds_evals.testing import program_block
from imbue.minds_evals.testing import workspace_state_output

_REGISTRY_TOML = (
    '[[apps]]\nname = "system_interface"\nurl = "http://localhost:8000"\nlabel = "system_interface-aa"\n\n'
    '[[apps]]\nname = "todo"\nurl = "http://localhost:8081"\nlabel = "todo-bb"\n'
)
_SERVICES_TEXT = (
    "system_interface                 RUNNING   pid 101, uptime 0:10:00\n"
    "todo                             RUNNING   pid 103, uptime 0:05:00\n"
)


# The registry row is joined to its program through the forward_port.py call in the program's block,
# which is how a multi-port app's extra origin rows and a renamed program both resolve correctly.
_SUPERVISORD_CONF = (
    "[program:system_interface]\n"
    'command=bash -c "python3 system/scripts/forward_port.py --url http://localhost:8000 '
    '--name system_interface && system-interface"\n'
    "\n"
    "[program:todo]\n"
    'command=bash -c "python3 system/scripts/forward_port.py --url http://localhost:8081 '
    '--name todo && uv run todo"\n'
)


def _authored(**overrides: object) -> Expectations:
    raw: dict[str, object] = {"outcome": "A working to-do web app.", "deliverable": {"kind": "minds-app"}}
    raw.update(overrides)
    return parse_expectations(raw, "todo-app")


def _case_config(expectations: Expectations | None, verification_timeout_seconds: float = 600.0) -> CaseConfig:
    return CaseConfig(
        case_id="todo-app",
        persona="Non-technical founder.",
        prompts=("Build it", "Sounds good."),
        timeout_seconds=1800.0,
        verification_timeout_seconds=verification_timeout_seconds,
        mngr_branch="main",
        mngr_sha="a" * 40,
        dwt_repo="https://example.invalid/dwt.git",
        dwt_branch="main",
        dwt_sha="d" * 40,
        avg_word_count_baseline=100.0,
        expectations=expand_expectations(expectations) if expectations is not None else None,
        authored_expectations=expectations,
    )


def _http_check(target: str = "registered-apps", expect_status: int = 200, expect_body_regex: str = "") -> HttpCheck:
    return HttpCheck(
        check_id="http_0", target=target, expect_status=expect_status, expect_body_regex=expect_body_regex
    )


# --- pure parsing helpers ---


def test_parse_apps_registry_reads_names_urls_and_marks_preexisting_rows() -> None:
    apps = evidence_collection.parse_apps_registry(_REGISTRY_TOML, TEMPLATE_PREEXISTING_APPS)

    assert apps is not None
    assert [(app.name, app.url, app.is_preexisting) for app in apps] == [
        ("system_interface", "http://localhost:8000", True),
        ("todo", "http://localhost:8081", False),
    ]


def test_the_templates_own_file_browser_is_not_a_deliverable() -> None:
    # `files` ships with the workspace template and registers through exactly the path a delivered
    # app does, so nothing about its row says otherwise. Counting it charges the agent for an app it
    # never wrote, and -- when that app is unhealthy -- aims the UI flows at a dead port.
    apps = evidence_collection.parse_apps_registry(
        '[[apps]]\nname = "files"\nurl = "http://localhost:8300"\nlabel = "files-aa"\n', TEMPLATE_PREEXISTING_APPS
    )

    assert apps is not None
    assert [(app.name, app.is_preexisting) for app in apps] == [("files", True)]


@pytest.mark.parametrize("registry_text", ["not = [toml", 'apps = "wrong shape"'])
def test_parse_apps_registry_reports_an_unreadable_registry_as_none(registry_text: str) -> None:
    # None ("could not read it") and () ("read it; it lists nothing") are different claims: the
    # second is the agent shipping nothing, which must score against the agent.
    assert evidence_collection.parse_apps_registry(registry_text, TEMPLATE_PREEXISTING_APPS) is None


@pytest.mark.parametrize("registry_text", ["", "   ", "other = 1"])
def test_parse_apps_registry_reports_a_readable_but_empty_registry_as_no_apps(registry_text: str) -> None:
    assert evidence_collection.parse_apps_registry(registry_text, TEMPLATE_PREEXISTING_APPS) == ()


def test_parse_service_states_reads_program_states() -> None:
    assert evidence_collection.parse_service_states(_SERVICES_TEXT) == {
        "system_interface": "RUNNING",
        "todo": "RUNNING",
    }


@pytest.mark.parametrize(
    "services_text",
    [
        "",
        "bash: supervisorctl: command not found",
        "unix:///var/run/supervisor.sock refused connection",
        "error: <class 'FileNotFoundError'>, [Errno 2] No such file",
    ],
)
def test_parse_service_states_yields_nothing_for_an_error_message(services_text: str) -> None:
    # `supervisorctl status` exits nonzero merely because a program is down, so only the content
    # can tell a real listing from a broken instrument -- and a broken one must not read as a fleet
    # of stopped services, which would be charged to the agent.
    assert evidence_collection.parse_service_states(services_text) == {}


def test_service_entries_find_a_grouped_supervisord_program() -> None:
    # supervisorctl prints a grouped program as `group:process`; a bare-name lookup alone would read
    # a perfectly healthy grouped service as absent and score it against the agent.
    delivered = (RegisteredApp(name="todo", url="http://localhost:8081", is_preexisting=False, is_internal=False),)

    entries = evidence_collection.service_entries(
        "app_registered", delivered, {"apps:todo": "RUNNING"}, {"todo": "todo"}, True
    )

    assert entries[0].status is CheckStatus.PASSED


def test_split_sections_keeps_the_first_occurrence_of_a_marker() -> None:
    # An HTTP body and a test command's output are agent-controlled and are emitted after their
    # markers, so a later duplicate must never overwrite an earlier, harness-emitted section.
    forged = probe_sections(status="200 0.01\n", body="") + probe_sections(status="500 9.9\n")

    assert evidence_collection.split_sections(forged)["status"] == "200 0.01\n"


def test_split_sections_separates_one_commands_several_answers() -> None:
    assert evidence_collection.split_sections(
        "noise\n" + probe_sections(repo_root="/home/user/workspace\n", registry="x = 1\n", services="")
    ) == {"repo_root": "/home/user/workspace\n", "registry": "x = 1\n", "services": ""}


@pytest.mark.parametrize(
    ("status_section", "expected"),
    [("200 0.0041", (200, 0.004)), ("000 0.000153", (0, 0.0)), ("", (0, 0.0)), ("garbage", (0, 0.0))],
)
def test_parse_curl_status_reads_code_and_timing(status_section: str, expected: tuple[int, float]) -> None:
    assert evidence_collection.parse_curl_status(status_section) == expected


def test_http_entry_status_separates_a_broken_instrument_from_a_broken_app() -> None:
    # Not being able to ask means we could not find out (ERROR); an app answering wrong -- or
    # refusing the connection, which curl reports as 000 -- is the workspace falling short (FAILED).
    # The whole grading policy rests on that distinction.
    assert evidence_collection.http_entry_status(False, "", 200, "ok", _http_check()) == (
        CheckStatus.ERROR,
        evidence_collection.REASON_BRIDGE_FAILED,
    )
    assert evidence_collection.http_entry_status(True, "curl_missing", 0, "", _http_check()) == (
        CheckStatus.ERROR,
        evidence_collection.REASON_PROBE_UNAVAILABLE,
    )
    assert evidence_collection.http_entry_status(True, "", 500, "boom", _http_check()) == (
        CheckStatus.FAILED,
        evidence_collection.REASON_WRONG_STATUS,
    )
    assert evidence_collection.http_entry_status(True, "", 0, "", _http_check()) == (
        CheckStatus.FAILED,
        evidence_collection.REASON_WRONG_STATUS,
    )
    assert evidence_collection.http_entry_status(True, "", 200, "ok", _http_check()) == (CheckStatus.PASSED, "")


def test_http_entry_status_checks_a_declared_body_regex() -> None:
    check = _http_check(expect_body_regex="buy milk")

    assert evidence_collection.http_entry_status(True, "", 200, "<p>buy milk</p>", check) == (CheckStatus.PASSED, "")
    assert evidence_collection.http_entry_status(True, "", 200, "<p>nope</p>", check) == (
        CheckStatus.FAILED,
        evidence_collection.REASON_BODY_MISMATCH,
    )


def test_resolve_http_targets_fans_registered_apps_out_over_delivered_apps_only() -> None:
    apps = (
        RegisteredApp(name="terminal", url="http://localhost:7681", is_preexisting=True, is_internal=False),
        RegisteredApp(name="todo", url="http://localhost:8081", is_preexisting=False, is_internal=False),
        RegisteredApp(name="halfway", url="", is_preexisting=False, is_internal=False),
    )

    delivered = evidence_collection.resolve_delivered_apps(apps, frozenset())
    assert [app.name for app in evidence_collection.resolve_http_targets(_http_check(), delivered)] == ["todo"]
    assert [app.name for app in evidence_collection.resolve_http_targets(_http_check(target="todo"), delivered)] == [
        "todo"
    ]
    assert evidence_collection.resolve_http_targets(_http_check(target="absent"), delivered) == ()


def test_registration_entry_distinguishes_an_unresolved_registry_from_an_empty_one() -> None:
    delivered = (RegisteredApp(name="todo", url="http://localhost:8081", is_preexisting=False, is_internal=False),)

    absent = evidence_collection.registration_entry(
        "app_registered", 1, None, evidence_collection.REASON_REGISTRY_ABSENT
    )
    unreadable = evidence_collection.registration_entry(
        "app_registered", 1, None, evidence_collection.REASON_REGISTRY_UNREADABLE
    )
    # An empty registry is the agent shipping nothing -- the failure this whole eval exists to catch
    # -- so it must score against the agent rather than error the trial.
    empty = evidence_collection.registration_entry("app_registered", 1, (), "")

    assert (absent.status, absent.reason) == (CheckStatus.ERROR, evidence_collection.REASON_REGISTRY_ABSENT)
    assert (unreadable.status, unreadable.reason) == (
        CheckStatus.ERROR,
        evidence_collection.REASON_REGISTRY_UNREADABLE,
    )
    assert (empty.status, empty.reason) == (CheckStatus.FAILED, evidence_collection.REASON_TOO_FEW_APPS)
    assert evidence_collection.registration_entry("app_registered", 1, delivered, "").status is CheckStatus.PASSED
    assert (
        evidence_collection.registration_entry("app_registered", 2, delivered, "").reason
        == evidence_collection.REASON_TOO_FEW_APPS
    )


def test_service_entries_flag_a_registered_app_whose_service_is_not_running() -> None:
    delivered = (RegisteredApp(name="todo", url="http://localhost:8081", is_preexisting=False, is_internal=False),)

    programs = {"todo": "todo"}
    running = evidence_collection.service_entries("app_registered", delivered, {"todo": "RUNNING"}, programs, True)
    crashed = evidence_collection.service_entries("app_registered", delivered, {"todo": "FATAL"}, programs, True)
    unknown = evidence_collection.service_entries("app_registered", delivered, {}, programs, False)

    assert running[0].status is CheckStatus.PASSED
    assert crashed[0].status is CheckStatus.FAILED
    assert crashed[0].reason == evidence_collection.REASON_SERVICE_NOT_RUNNING
    assert unknown[0].status is CheckStatus.ERROR


def test_parse_supervised_registrations_joins_rows_to_the_program_that_registers_them() -> None:
    # Not name equality: a multi-port app registers extra origin rows from one program, and a
    # program is free to register under a name other than its own.
    conf = (
        "[program:system_interface]\n"
        "command=python3 system/scripts/forward_port.py --url http://localhost:8000 --name system_interface\n"
        "\n"
        "[program:dashboard]\n"
        "command=bash -c 'python3 system/scripts/forward_port.py --url http://localhost:9000 --name shop && "
        "python3 system/scripts/forward_port.py --url http://localhost:9001 --name shop-admin && uv run shop'\n"
        "\n"
        "[program:cron]\ncommand=cron -f\n"
    )

    assert evidence_collection.parse_supervised_registrations(conf) == {
        "system_interface": "system_interface",
        "shop": "dashboard",
        "shop-admin": "dashboard",
    }


def test_parse_supervised_registrations_accepts_either_flag_order() -> None:
    # The app scaffold writes --url first; the isolated-instance runner writes --name first.
    conf = "[program:todo]\ncommand=python3 system/scripts/forward_port.py --name todo --url http://localhost:8081\n"

    assert evidence_collection.parse_supervised_registrations(conf) == {"todo": "todo"}


def test_the_config_half_names_only_the_apps_it_registers_itself() -> None:
    # The config half of the pre-existing set, joined through the forward_port.py calls in the file
    # rather than read off a hand-kept name list -- which is what keeps it correct as the template
    # gains and loses apps. An app that registers from inside its program's script is not here at
    # all; the registry half is what covers those.
    config_registrations = frozenset(evidence_collection.parse_supervised_registrations(TEMPLATE_SUPERVISORD_CONF))

    assert config_registrations == TEMPLATE_CONFIG_REGISTRATIONS
    assert not config_registrations & SCRIPT_REGISTERED_APPS


@pytest.mark.parametrize(
    ("registry_names", "config_registrations", "expected"),
    [
        pytest.param(
            frozenset({"system_interface", "terminal"}),
            frozenset({"system_interface", "files", "browser"}),
            frozenset({"system_interface", "terminal", "files", "browser"}),
            id="the-boot-snapshot-catches-an-app-the-config-never-names",
        ),
        pytest.param(
            frozenset({"system_interface"}),
            frozenset({"system_interface", "browser"}),
            frozenset({"system_interface", "browser"}),
            id="the-config-half-covers-an-app-that-had-not-registered-by-snapshot-time",
        ),
        pytest.param(
            frozenset({"terminal"}),
            frozenset(),
            frozenset({"terminal"}),
            id="the-registry-half-alone-is-enough",
        ),
        pytest.param(
            None,
            frozenset({"system_interface"}),
            None,
            id="without-the-registry-half-nothing-can-be-called-preexisting",
        ),
    ],
)
def test_resolve_preexisting_registrations_unions_both_halves_and_needs_the_registry(
    registry_names: frozenset[str] | None, config_registrations: frozenset[str], expected: frozenset[str] | None
) -> None:
    # The cases pin what each half contributes and that the registry half is the one that must be
    # readable; see `resolve_preexisting_registrations` for why neither is complete alone.
    assert evidence_collection.resolve_preexisting_registrations(registry_names, config_registrations) == expected


def test_parse_registry_names_separates_an_unreadable_registry_from_an_empty_one() -> None:
    assert evidence_collection.parse_registry_names(_REGISTRY_TOML) == frozenset({"system_interface", "todo"})
    assert evidence_collection.parse_registry_names("") == frozenset()
    assert evidence_collection.parse_registry_names("not = [toml") is None


def test_parse_registry_snapshot_reads_names_only_from_a_registry_that_is_there() -> None:
    # Absent, unparseable, and no probe output at all each leave the snapshot unknown, never empty.
    # The first case resolves from the registry alone: none of these outputs carries a config
    # section, and only a missing registry makes the set unknown.
    present = workspace_state_output(_REGISTRY_TOML)
    assert evidence_collection.parse_registry_snapshot(present) == frozenset({"system_interface", "todo"})
    assert evidence_collection.parse_registry_snapshot(workspace_state_output("", registry_status="absent")) is None
    unparseable = workspace_state_output("not = [toml")
    assert evidence_collection.parse_registry_snapshot(unparseable) is None
    assert evidence_collection.parse_registry_snapshot("") is None


def test_parse_registry_snapshot_takes_both_halves_from_the_one_probe() -> None:
    # One probe, both halves: here the registry knows only the script-registered rows and the config
    # knows only the rest, so the snapshot has to end up with both.
    registry = "".join(
        '[[apps]]\nname = "{}"\nurl = "http://localhost:7681"\n\n'.format(name)
        for name in sorted(SCRIPT_REGISTERED_APPS)
    )
    output = workspace_state_output(registry, supervisord=TEMPLATE_SUPERVISORD_CONF)

    assert evidence_collection.parse_registry_snapshot(output) == TEMPLATE_PREEXISTING_APPS


def test_a_workspace_serving_an_extra_app_at_boot_makes_it_preexisting() -> None:
    # An eval config may point dwt_repo/dwt_branch at a fork that ships apps stock dwt does not.
    # Those are still there before the agent runs, so they are not the case's deliverable.
    forked_conf = TEMPLATE_SUPERVISORD_CONF + program_block("notes", ("notes", "http://localhost:8400"))
    preexisting = evidence_collection.parse_registry_snapshot(
        workspace_state_output(_REGISTRY_TOML, supervisord=forked_conf)
    )

    assert preexisting is not None
    apps = evidence_collection.parse_apps_registry(
        '[[apps]]\nname = "notes"\nurl = "http://localhost:8400"\n\n'
        '[[apps]]\nname = "todo2"\nurl = "http://localhost:8081"\n',
        preexisting,
    )
    assert apps is not None
    assert [app.name for app in evidence_collection.resolve_delivered_apps(apps, frozenset())] == ["todo2"]


def test_parse_isolated_instance_services_reads_every_concatenated_state_file() -> None:
    # The probe cats every instance.json, so the parser decodes one object at a time.
    instances = (
        json.dumps({"services": ["shop-preview", "si-preview"], "pids": [1, 2]})
        + "\n"
        + json.dumps({"services": ["scratch"]})
        + "\n"
    )

    assert evidence_collection.parse_isolated_instance_services(instances) == frozenset(
        {"shop-preview", "si-preview", "scratch"}
    )


@pytest.mark.parametrize("instances_text", ["", "   ", "not json at all"])
def test_parse_isolated_instance_services_tolerates_no_state(instances_text: str) -> None:
    assert evidence_collection.parse_isolated_instance_services(instances_text) == frozenset()


def test_parse_apps_registry_reads_the_internal_marker() -> None:
    # Verbatim shape from a live workspace: the owner-exec daemon forwards a port but has no page.
    registry = (
        '[[apps]]\nname = "owner-exec"\nurl = "http://127.0.0.1:8793"\nlabel = "owner-exec-75o5av89"\n'
        "internal = true\n\n"
        '[[apps]]\nname = "todo-list"\nurl = "http://localhost:8080"\nlabel = "todo-list-nyk8ptte"\n'
    )

    apps = evidence_collection.parse_apps_registry(registry, TEMPLATE_PREEXISTING_APPS)

    assert apps is not None
    assert [(app.name, app.is_internal) for app in apps] == [("owner-exec", True), ("todo-list", False)]


def test_resolve_delivered_apps_excludes_internal_machinery() -> None:
    # Regression from a live trial: owner-exec is registered but marked internal, and answers 404 on
    # its root by design. Counted as delivered it both inflated the app count and failed the implied
    # root-path probe -- charging the agent for a daemon it never shipped.
    apps = (
        RegisteredApp(name="owner-exec", url="http://127.0.0.1:8793", is_preexisting=False, is_internal=True),
        RegisteredApp(name="todo-list", url="http://localhost:8080", is_preexisting=False, is_internal=False),
    )

    delivered = evidence_collection.resolve_delivered_apps(apps, frozenset())

    assert [app.name for app in delivered] == ["todo-list"]


def test_service_entries_fall_back_to_a_program_named_like_the_row() -> None:
    # Covers a service that registers its port at runtime rather than through a forward_port call in
    # supervisord.conf, so the config join finds nothing but the program plainly exists.
    delivered = (
        RegisteredApp(name="todo-list", url="http://localhost:8080", is_preexisting=False, is_internal=False),
    )

    entries = evidence_collection.service_entries("app_registered", delivered, {"todo-list": "RUNNING"}, {}, True)

    assert entries[0].status is CheckStatus.PASSED


def test_resolve_delivered_apps_excludes_abandoned_preview_rows() -> None:
    # A throwaway preview registers through the same path and leaves its row behind when abandoned.
    # Counting it would both satisfy app_registered on something that was never the deliverable and
    # fail the root-path probe on its dead port.
    apps = (
        RegisteredApp(name="terminal", url="http://localhost:7681", is_preexisting=True, is_internal=False),
        RegisteredApp(name="shop", url="http://localhost:9000", is_preexisting=False, is_internal=False),
        RegisteredApp(name="shop-preview", url="http://localhost:9100", is_preexisting=False, is_internal=False),
    )

    delivered = evidence_collection.resolve_delivered_apps(apps, frozenset({"shop-preview"}))

    assert [app.name for app in delivered] == ["shop"]


def test_resolve_delivered_apps_keeps_a_real_app_whose_name_looks_like_a_preview() -> None:
    # Exclusion is by the instance runner's own record, not by name pattern: instance names are
    # caller-supplied, so a pattern would drop a genuine deliverable that happens to be named this
    # way and still miss throwaways named anything else.
    apps = (RegisteredApp(name="recipes-test", url="http://localhost:9000", is_preexisting=False, is_internal=False),)

    assert [app.name for app in evidence_collection.resolve_delivered_apps(apps, frozenset())] == ["recipes-test"]


def test_service_entries_flag_a_registry_row_no_program_supervises() -> None:
    # An app started by hand and never wired into supervisord would not survive a restart, which is
    # a real shortfall of the minds-app contract -- recorded under its own reason so it stays
    # distinguishable from a program that exists and crashed.
    delivered = (RegisteredApp(name="handmade", url="http://localhost:9000", is_preexisting=False, is_internal=False),)

    entries = evidence_collection.service_entries(
        "app_registered", delivered, {"todo": "RUNNING"}, {"todo": "todo"}, True
    )

    assert entries[0].status is CheckStatus.FAILED
    assert entries[0].reason == evidence_collection.REASON_NO_SUPERVISED_PROGRAM


def test_service_entries_resolve_a_program_named_differently_from_the_row() -> None:
    delivered = (
        RegisteredApp(name="shop-admin", url="http://localhost:9001", is_preexisting=False, is_internal=False),
    )

    entries = evidence_collection.service_entries(
        "app_registered", delivered, {"dashboard": "RUNNING"}, {"shop-admin": "dashboard"}, True
    )

    assert entries[0].status is CheckStatus.PASSED


def test_inventory_excludes_git_on_top_of_the_snapshot_excludes() -> None:
    # Loose objects would crowd real deliverable files out of the entry cap; the committed history
    # travels as the git bundle instead.
    assert ".git" in evidence_collection.INVENTORY_EXCLUDES
    assert "node_modules" in evidence_collection.INVENTORY_EXCLUDES


def test_test_command_wrapper_runs_the_command_in_a_subshell() -> None:
    # A declared test command ending in `exit` would otherwise take the probe down with it, losing
    # the exit code and output it exists to record.
    command = evidence_collection.test_command_wrapper("/home/user/workspace", "pytest -q")

    assert "( pytest -q )" in command


# --- the collector against a scripted workspace ---


def _collector_rules(
    registry_text: str = _REGISTRY_TOML,
    services_text: str = _SERVICES_TEXT,
    http_status: str = "200 0.0041",
    test_exit_code: str = "0",
    is_inventory_pulled: bool = True,
    registry_status: str = "present",
    supervisord_conf: str = _SUPERVISORD_CONF,
    isolated_instances: str = "",
) -> list[ScriptedExecRule]:
    probe = workspace_state_output(
        registry_text,
        registry_status=registry_status,
        services=services_text,
        supervisord=supervisord_conf,
        isolated_instances=isolated_instances,
    )
    repo_state = probe_sections(head_sha="b" * 40 + "\n", status="", commit_count="3\n", bundle="")
    http = probe_sections(status=http_status + "\n", headers="HTTP/1.1 200 OK\r\n", body="<h1>todo</h1>")
    test_result = probe_sections(exit_code=test_exit_code + "\n", output="1 passed\n")
    return [
        ScriptedExecRule("MINDS_EVALS_SECTION:repo_root", [ok_result(mngr_exec_json(probe))]),
        ScriptedExecRule("base64 -d | python3 -", [ok_result(mngr_exec_json("2\n"))]),
        ScriptedExecRule("git bundle create", [ok_result(mngr_exec_json(repo_state))]),
        ScriptedExecRule("test_out", [ok_result(mngr_exec_json(test_result))]),
        ScriptedExecRule("http_headers", [ok_result(mngr_exec_json(http))]),
        ScriptedExecRule("mngr rsync", [ok_result() if is_inventory_pulled else failed_result()]),
    ]


def _run_collector(
    tmp_path: Path,
    case: CaseConfig,
    rules: list[ScriptedExecRule],
    deadline_offset_seconds: float = 600.0,
    is_expectations_collection_wanted: bool = True,
    preexisting_registrations: frozenset[str] | None = TEMPLATE_PREEXISTING_APPS,
) -> tuple[evidence_collection.EvidenceCollector, MockBoxEnvironment]:
    environment = MockBoxEnvironment(tmp_path, rules)
    logs_dir = tmp_path / "agent"
    logs_dir.mkdir(parents=True, exist_ok=True)
    collector = evidence_collection.EvidenceCollector(
        environment=environment,
        box_env={"MINDS_ENV": "staging"},
        workspace_agent_id="ws-1",
        case=case,
        clone_base_sha="a" * 40,
        dwt_tip_sha="e" * 40,
        preexisting_registrations=preexisting_registrations,
        host_logs_dir=logs_dir,
        deadline=time.monotonic() + deadline_offset_seconds,
    )
    asyncio.run(collector.collect(is_expectations_collection_wanted=is_expectations_collection_wanted))
    return collector, environment


def _entry_status_by_id(collector: evidence_collection.EvidenceCollector) -> dict[str, CheckStatus]:
    return {entry.entry_id: entry.status for entry in collector.entries}


def test_collector_records_every_declared_check_as_passed_for_a_healthy_workspace(tmp_path: Path) -> None:
    case = _case_config(_authored(test_commands=["uv run pytest -q"]))

    collector, environment = _run_collector(tmp_path, case, _collector_rules())

    assert _entry_status_by_id(collector) == {
        "file_inventory": CheckStatus.PASSED,
        "deliverable_bundle": CheckStatus.PASSED,
        "test_command_0": CheckStatus.PASSED,
        "http_0_registered_apps_todo": CheckStatus.PASSED,
        "app_registered": CheckStatus.PASSED,
        "app_registered_service_todo": CheckStatus.PASSED,
    }
    manifest = collector.manifest()
    assert manifest.is_evidence_complete is True
    assert {phase.name for phase in manifest.phases} == {
        "workspace_state",
        "file_inventory",
        "repo_state",
        "test_commands",
        "http_probes",
        "app_checks",
    }

    # The bundle reached the box, where the task's declared artifact directory picks it up.
    uploaded = environment.uploaded_content_by_target
    assert json.loads(uploaded["/logs/agent/verification/manifest.json"])["is_evidence_complete"] is True
    assert uploaded["/logs/agent/verification/apps.toml"] == _REGISTRY_TOML
    assert uploaded["/logs/agent/verification/services.txt"] == _SERVICES_TEXT
    assert json.loads(uploaded["/logs/agent/verification/repo_state.json"])["head_sha"] == "b" * 40
    # The check id is part of the evidence filename, so two checks aimed at one app cannot collide.
    probe = json.loads(uploaded["/logs/agent/verification/http/http_0_registered_apps_0_todo.json"])
    assert (probe["status_code"], probe["probe_error"]) == (200, "")


def test_collector_marks_a_crashed_service_and_a_bad_response_as_workspace_failures(tmp_path: Path) -> None:
    case = _case_config(_authored())
    rules = _collector_rules(
        services_text="system_interface   RUNNING   pid 101\ntodo   FATAL   Exited too quickly\n",
        http_status="500 0.01",
    )

    collector, _environment = _run_collector(tmp_path, case, rules)

    statuses = _entry_status_by_id(collector)
    assert statuses["app_registered_service_todo"] is CheckStatus.FAILED
    assert statuses["http_0_registered_apps_todo"] is CheckStatus.FAILED
    # The workspace fell short, but the evidence itself is complete: nothing is charged to the harness.
    assert collector.manifest().is_evidence_complete is True


def test_collector_scores_a_workspace_that_registered_nothing_against_the_agent(tmp_path: Path) -> None:
    # The ships-nothing case: the registry is there and lists no delivered app. This is the exact
    # failure the eval exists to catch, so it must be a scored FAILED, never an ERROR that would
    # make finalize.py abandon the whole grade as a harness problem.
    case = _case_config(_authored())
    preexisting_only = '[[apps]]\nname = "system_interface"\nurl = "http://localhost:8000"\n'

    collector, _environment = _run_collector(tmp_path, case, _collector_rules(registry_text=preexisting_only))

    statuses = _entry_status_by_id(collector)
    assert statuses["app_registered"] is CheckStatus.FAILED
    assert statuses["http_0_registered_apps"] is CheckStatus.FAILED
    assert collector.manifest().is_evidence_complete is True


def test_collector_ignores_an_abandoned_preview_row(tmp_path: Path) -> None:
    # An abandoned throwaway leaves a registry row behind with nothing serving it. Counted as
    # delivered it would both satisfy app_registered and fail the root-path probe -- so the case
    # would score against the agent for a server that was never the deliverable.
    case = _case_config(_authored())
    registry = _REGISTRY_TOML + '\n[[apps]]\nname = "todo-preview"\nurl = "http://localhost:9100"\n'
    rules = _collector_rules(
        registry_text=registry,
        isolated_instances=json.dumps({"services": ["todo-preview"], "pids": [42]}),
    )

    collector, _environment = _run_collector(tmp_path, case, rules)

    statuses = _entry_status_by_id(collector)
    assert "app_registered_service_todo_preview" not in statuses
    assert "http_0_registered_apps_todo_preview" not in statuses
    assert statuses["app_registered"] is CheckStatus.PASSED
    assert statuses["http_0_registered_apps_todo"] is CheckStatus.PASSED


def test_collector_publishes_the_preexisting_set_it_excluded(tmp_path: Path) -> None:
    # A manifest reader can see what was subtracted from the registry instead of having to infer it.
    case = _case_config(_authored())

    collector, _environment = _run_collector(tmp_path, case, _collector_rules())

    assert collector.manifest().preexisting_registrations == tuple(sorted(TEMPLATE_PREEXISTING_APPS))


def test_collector_scores_an_app_the_workspace_already_served_as_no_delivery(tmp_path: Path) -> None:
    # The registry lists one app, but the workspace was already serving it before the agent ran, so
    # the agent delivered nothing -- which is agent-side evidence, not a harness error.
    case = _case_config(_authored())
    registry = '[[apps]]\nname = "notes"\nurl = "http://localhost:8400"\nlabel = "notes-aa"\n'

    collector, _environment = _run_collector(
        tmp_path,
        case,
        _collector_rules(registry_text=registry),
        preexisting_registrations=TEMPLATE_PREEXISTING_APPS | {"notes"},
    )

    statuses = _entry_status_by_id(collector)
    assert statuses["app_registered"] is CheckStatus.FAILED
    assert "app_registered_service_notes" not in statuses
    assert collector.manifest().is_evidence_complete is True


def test_collector_cannot_score_apps_without_a_preexisting_set(tmp_path: Path) -> None:
    # Without a pre-existing set there is no way to tell what the agent added from what booted with
    # the workspace. That is the instrument failing, so every entry whose meaning depends on the
    # distinction is unmeasured -- never a failure charged to the agent.
    case = _case_config(_authored())

    collector, _environment = _run_collector(tmp_path, case, _collector_rules(), preexisting_registrations=None)

    entries_by_id = {entry.entry_id: entry for entry in collector.entries}
    assert entries_by_id["app_registered"].status is CheckStatus.ERROR
    assert entries_by_id["app_registered"].reason == evidence_collection.REASON_PREEXISTING_UNKNOWN
    assert entries_by_id["http_0_registered_apps"].status is CheckStatus.ERROR
    assert entries_by_id["http_0_registered_apps"].reason == evidence_collection.REASON_PREEXISTING_UNKNOWN
    assert not any(entry.status is CheckStatus.FAILED for entry in collector.entries)
    manifest = collector.manifest()
    assert manifest.is_evidence_complete is False
    assert manifest.preexisting_registrations is None


def test_collector_still_captures_the_registry_without_a_preexisting_set(tmp_path: Path) -> None:
    # The unconditional capture is what makes a trial diagnosable after the fact, so it must not be
    # gated on being able to resolve the delivered set.
    case = _case_config(_authored())

    _collector, environment = _run_collector(tmp_path, case, _collector_rules(), preexisting_registrations=None)

    uploaded = environment.uploaded_content_by_target
    assert uploaded["/logs/agent/verification/apps.toml"] == _REGISTRY_TOML
    assert uploaded["/logs/agent/verification/services.txt"] == _SERVICES_TEXT


def test_collector_records_an_absent_registry_as_an_error_not_a_failure(tmp_path: Path) -> None:
    case = _case_config(_authored())

    collector, _environment = _run_collector(
        tmp_path, case, _collector_rules(registry_status="absent", registry_text="", services_text="")
    )

    statuses = _entry_status_by_id(collector)
    assert statuses["app_registered"] is CheckStatus.ERROR
    # With no registry there is no address to probe either, so the probe cannot be charged to the app.
    assert statuses["http_0_registered_apps"] is CheckStatus.ERROR
    assert collector.manifest().is_evidence_complete is False


def test_collector_records_a_broken_supervisorctl_as_an_error_not_a_dead_service(tmp_path: Path) -> None:
    case = _case_config(_authored())

    collector, _environment = _run_collector(
        tmp_path, case, _collector_rules(services_text="bash: supervisorctl: command not found\n")
    )

    statuses = _entry_status_by_id(collector)
    assert statuses["app_registered_service_todo"] is CheckStatus.ERROR
    # The registry itself was readable, so registration is still a real, scored verdict.
    assert statuses["app_registered"] is CheckStatus.PASSED


def test_collector_records_a_failed_inventory_pull_as_an_error(tmp_path: Path) -> None:
    case = _case_config(_authored())

    collector, _environment = _run_collector(tmp_path, case, _collector_rules(is_inventory_pulled=False))

    assert _entry_status_by_id(collector)["file_inventory"] is CheckStatus.ERROR
    assert collector.manifest().is_evidence_complete is False


def test_collector_records_a_ships_nothing_trial_as_evidence_not_an_error(tmp_path: Path) -> None:
    # `git bundle create` refuses an empty range (exit 128), so the agent-committed-nothing case has
    # to be handled before the call, not after. It is the ships-nothing outcome this eval exists to
    # catch: it must read as recorded agent-side evidence, never as the harness failing to measure.
    case = _case_config(_authored())
    zero_commit_repo_state = probe_sections(
        head_sha="b" * 40 + "\n", status="", commit_count="0\n", bundle="no-commits"
    )
    rules = _collector_rules()
    rules[2] = ScriptedExecRule("git bundle create", [ok_result(mngr_exec_json(zero_commit_repo_state))])

    collector, environment = _run_collector(tmp_path, case, rules)

    bundle_entry = next(entry for entry in collector.entries if entry.entry_id == "deliverable_bundle")
    assert bundle_entry.status is CheckStatus.PASSED
    assert bundle_entry.reason == ""
    assert "0 commit(s)" in bundle_entry.detail
    # No bundle was written, so nothing claims one exists.
    assert bundle_entry.evidence_path == ""
    assert collector.manifest().is_evidence_complete is True
    repo_state = json.loads(environment.uploaded_content_by_target["/logs/agent/verification/repo_state.json"])
    assert repo_state["commit_count_beyond_base"] == "0"


def test_repo_state_command_never_invokes_git_bundle_on_an_empty_range() -> None:
    # The guard is in the shell, because the failure it prevents is git's own refusal to bundle an
    # empty range -- which would surface as a collection error rather than as the agent's outcome.
    command = evidence_collection.repo_state_command("/home/user/workspace", "a" * 40)

    assert "no-commits" in command
    # A non-numeric or zero count short-circuits before `git bundle create` is ever reached.
    assert command.index("no-commits") < command.index("git bundle create")


def test_collector_records_a_failing_test_command_without_erroring(tmp_path: Path) -> None:
    case = _case_config(_authored(test_commands=["uv run pytest -q"]))

    collector, _environment = _run_collector(tmp_path, case, _collector_rules(test_exit_code="1"))

    failed = next(entry for entry in collector.entries if entry.entry_id == "test_command_0")
    assert failed.status is CheckStatus.FAILED
    assert failed.reason == evidence_collection.REASON_NONZERO_EXIT
    assert failed.check_class is CheckClass.TEST_COMMAND


def test_collector_records_timeouts_as_errors_rather_than_failures(tmp_path: Path) -> None:
    case = _case_config(_authored(test_commands=["uv run pytest -q"]))

    collector, _environment = _run_collector(tmp_path, case, _collector_rules(), deadline_offset_seconds=-1.0)

    statuses = _entry_status_by_id(collector)
    assert statuses["test_command_0"] is CheckStatus.ERROR
    assert statuses["http_0_registered_apps_todo"] is CheckStatus.ERROR
    timed_out = next(entry for entry in collector.entries if entry.entry_id == "test_command_0")
    assert timed_out.reason == evidence_collection.REASON_TIMEOUT


def test_collector_runs_only_the_always_on_capture_for_an_unfinished_trial(tmp_path: Path) -> None:
    case = _case_config(_authored(test_commands=["uv run pytest -q"]))

    collector, environment = _run_collector(
        tmp_path, case, _collector_rules(), is_expectations_collection_wanted=False
    )

    # The gates already zero an unfinished trial, so probing its build buys nothing -- but the cheap
    # registry/service/inventory capture still runs, which is what makes the failure diagnosable.
    assert set(_entry_status_by_id(collector)) == {"file_inventory"}
    assert environment.uploaded_content_by_target["/logs/agent/verification/apps.toml"] == _REGISTRY_TOML
    assert "/logs/agent/verification/repo_state.json" not in environment.uploaded_content_by_target


def test_collector_captures_workspace_state_for_a_case_with_no_expectations(tmp_path: Path) -> None:
    collector, environment = _run_collector(tmp_path, _case_config(None), _collector_rules())

    assert set(_entry_status_by_id(collector)) == {"file_inventory"}
    assert environment.uploaded_content_by_target["/logs/agent/verification/apps.toml"] == _REGISTRY_TOML
    assert (
        json.loads(environment.uploaded_content_by_target["/logs/agent/verification/manifest.json"])[
            "is_expectations_declared"
        ]
        is False
    )


def test_collector_writes_the_record_incrementally(tmp_path: Path) -> None:
    case = _case_config(_authored())

    _collector, environment = _run_collector(tmp_path, case, _collector_rules())

    # The manifest is rewritten after every phase, so a crash mid-phase still leaves a readable
    # record; the mock keeps only the last write, so assert the trace grew to cover every command.
    trace_lines = environment.uploaded_content_by_target["/logs/agent/verification/trace.jsonl"].splitlines()
    phases = [json.loads(line)["phase"] for line in trace_lines]
    assert phases[0] == "workspace_state"
    assert "http_probes" in phases
    assert all(isinstance(json.loads(line)["is_success"], bool) for line in trace_lines)


def test_collector_reports_a_dead_bridge_as_an_error(tmp_path: Path) -> None:
    case = _case_config(_authored())
    rules = [ScriptedExecRule("uv run mngr exec", [failed_result("bridge down")])]

    collector, _environment = _run_collector(tmp_path, case, rules)

    manifest = collector.manifest()
    assert manifest.is_evidence_complete is False
    assert {entry.entry_id for entry in manifest.entries if entry.status is CheckStatus.ERROR} >= {
        "workspace_state",
        "file_inventory",
        "deliverable_bundle",
    }


# --- the oracle's fabricated bundle ---


def test_oracle_evidence_files_record_every_declared_check_as_passed() -> None:
    case = _case_config(_authored(test_commands=["uv run pytest -q"]))

    files = evidence_collection.oracle_evidence_files(case)

    manifest = json.loads(files[evidence_collection.MANIFEST_FILENAME])
    assert manifest["is_evidence_complete"] is True
    assert {entry["status"] for entry in manifest["entries"]} == {"passed"}
    assert {entry["check_class"] for entry in manifest["entries"]} == {
        "files",
        "bundle",
        "app",
        "http",
        "test_command",
    }
    # Both SHAs travel, so a replay can regenerate the base clone and verify it before unbundling.
    assert manifest["base_sha"] and manifest["dwt_tip_sha"]
    assert json.loads(files[evidence_collection.REPO_STATE_FILENAME])["dwt_tip_sha"]
    # Read against the set the manifest itself publishes, so the fabricated bundle cannot claim one
    # exclusion and record another.
    registry_apps = evidence_collection.parse_apps_registry(
        files[evidence_collection.APPS_REGISTRY_FILENAME], frozenset(manifest["preexisting_registrations"])
    )
    assert registry_apps is not None
    assert [app.name for app in registry_apps if not app.is_preexisting] == ["delivered-app"]
    assert "RUNNING" in files[evidence_collection.SERVICES_FILENAME]


def test_oracle_evidence_inventory_satisfies_declared_file_globs() -> None:
    case = _case_config(_authored(deliverable={"kind": "minds-app", "files": [{"glob": "workspace/apps/*/main.py"}]}))

    inventory = evidence_collection.oracle_evidence_files(case)[evidence_collection.FILE_INVENTORY_FILENAME]

    paths = [json.loads(line)["path"] for line in inventory.splitlines()]
    assert any(path.startswith("workspace/apps/") and path.endswith("main.py") for path in paths)


# --- driving UI flows through the forwarded origin ---


_FLOWS = [
    {"name": "add-complete-delete", "steps": "Add 'buy milk'. Delete 'walk dog'.", "expect": "'buy milk' is visible."},
]
_TWO_FLOWS = [
    *_FLOWS,
    {"name": "persistence", "steps": "Add 'persist me'. Reload.", "expect": "'persist me' survived."},
]
_PAGE_SNAPSHOT = "- textbox 'Add a task'\n- button 'Add'"


def _step_result(
    is_ok: bool = True,
    reason: str = "",
    detail: str = "",
    snapshot: str = _PAGE_SNAPSHOT,
    screenshot_path: str = "/logs/agent/verification/flows/add_complete_delete/step_000.png",
) -> ExecResult:
    """What the box-side step script prints: one JSON object describing the step's outcome."""
    return ok_result(
        json.dumps(
            {
                "is_ok": is_ok,
                "reason": reason,
                "detail": detail,
                "url": "https://todo-x.{}.localhost:8431/".format(FAKE_WORKSPACE_AGENT_ID),
                "title": "Todo",
                "snapshot": snapshot,
                "screenshot_path": screenshot_path,
            }
        )
    )


def _executor_rules(
    forward_probe: str = "200",
    browser_probe: str = '{"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/x"}',
    step: ExecResult | None = None,
    is_browser_launched: bool = True,
) -> list[ScriptedExecRule]:
    """The box half of the scripted environment: the forward proxy, the browser, and the steps."""
    return [
        ScriptedExecRule("setsid nohup uv run mngr forward", [ok_result()]),
        ScriptedExecRule("https://127.0.0.1:8431/", [ok_result(forward_probe)]),
        ScriptedExecRule(
            "--remote-debugging-port", [ok_result("launched") if is_browser_launched else failed_result()]
        ),
        ScriptedExecRule("/json/version", [ok_result(browser_probe)]),
        ScriptedExecRule("box_flow_step.py", [step or _step_result()]),
        ScriptedExecRule("pkill -f", [ok_result()]),
    ]


def _flow_collector(
    tmp_path: Path,
    agent: ScriptedVerificationAgent | None,
    rules: list[ScriptedExecRule],
    flows: list[dict[str, str]] | None = None,
    agent_id: str = FAKE_WORKSPACE_AGENT_ID,
    preexisting_registrations: frozenset[str] | None = TEMPLATE_PREEXISTING_APPS,
) -> tuple[evidence_collection.EvidenceCollector, MockBoxEnvironment]:
    environment = MockBoxEnvironment(tmp_path, rules)
    logs_dir = tmp_path / "agent"
    logs_dir.mkdir(parents=True, exist_ok=True)
    collector = evidence_collection.EvidenceCollector(
        environment=environment,
        box_env={"MINDS_ENV": "staging"},
        workspace_agent_id=agent_id,
        case=_case_config(_authored(ui_flows=flows if flows is not None else _FLOWS)),
        clone_base_sha="a" * 40,
        dwt_tip_sha="e" * 40,
        preexisting_registrations=preexisting_registrations,
        host_logs_dir=logs_dir,
        deadline=time.monotonic() + 600.0,
        verification_agent=agent,
        verifier_model="claude-opus-4-8",
        readiness_poll_seconds=0.0,
        preauth_cookie=SecretStr("preauth-token"),
        browser_bridge_token=SecretStr("bridge-token"),
    )
    asyncio.run(collector.collect(is_expectations_collection_wanted=True))
    return collector, environment


def _run_flow_collector(
    tmp_path: Path,
    agent: ScriptedVerificationAgent,
    executor_rules: list[ScriptedExecRule] | None = None,
    flows: list[dict[str, str]] | None = None,
) -> tuple[evidence_collection.EvidenceCollector, MockBoxEnvironment]:
    return _flow_collector(tmp_path, agent, [*(executor_rules or _executor_rules()), *_collector_rules()], flows)


def _flow_entries(collector: evidence_collection.EvidenceCollector) -> list[ManifestEntry]:
    return [entry for entry in collector.entries if entry.check_class is CheckClass.UI_FLOWS]


def test_collector_drives_the_flow_at_the_apps_forwarded_origin(tmp_path: Path) -> None:
    # The whole point of this executor: the browser drives the app where the proxy serves it, at the
    # registry row's LABEL on the workspace's agent-keyed origin.
    agent = ScriptedVerificationAgent(actions=[click_action(), done_action()], readings=[reading()])

    collector, environment = _run_flow_collector(tmp_path, agent)

    entries = _flow_entries(collector)
    assert [(entry.entry_id, entry.status, entry.reason) for entry in entries] == [
        ("ui_flow_0_add_complete_delete", CheckStatus.PASSED, "")
    ]
    opening = next(command for command in environment.exec_commands if "box_flow_step.py" in command)
    # `todo-bb` is the row's LABEL; the service name is plain `todo`, so this also pins that the
    # URL is built from the label rather than the name.
    assert "https://todo-bb.{}.localhost:8431/".format(FAKE_WORKSPACE_AGENT_ID) in opening
    # The session cookie rides that first request, at the family scope the proxy issues its own at.
    assert "preauth-token" in opening
    assert "mngr_forward_session" in opening
    assert ".{}.localhost".format(FAKE_WORKSPACE_AGENT_ID) in opening


def test_the_flow_drives_an_app_that_actually_answers(tmp_path: Path) -> None:
    # With two delivered rows, the first-registered one can be a dead port -- and a dead port
    # serves the proxy's own error page, so a flow pointed at it would record the deliverable as
    # broken having never reached it.
    registry = (
        '[[apps]]\nname = "gallery"\nurl = "http://localhost:8300"\nlabel = "gallery-aa"\n\n'
        '[[apps]]\nname = "todo"\nurl = "http://localhost:8081"\nlabel = "todo-bb"\n'
    )
    services = (
        "gallery                          BACKOFF   Exited too quickly\n"
        "todo                             RUNNING   pid 103, uptime 0:05:00\n"
    )
    supervisord = (
        "[program:gallery]\n"
        'command=bash -c "python3 system/scripts/forward_port.py --url http://localhost:8300 '
        '--name gallery && uv run gallery"\n'
        "\n"
        "[program:todo]\n"
        'command=bash -c "python3 system/scripts/forward_port.py --url http://localhost:8081 '
        '--name todo && uv run todo"\n'
    )
    agent = ScriptedVerificationAgent(actions=[done_action()], readings=[reading()])
    rules = [
        *_executor_rules(),
        # The dead app is probed first and answers nothing; the todo app answers 200.
        *_collector_rules(registry_text=registry, services_text=services, supervisord_conf=supervisord),
    ]
    http_rule_index = next(index for index, rule in enumerate(rules) if rule.substring == "http_headers")
    rules[http_rule_index] = ScriptedExecRule(
        "http_headers",
        [
            ok_result(mngr_exec_json(probe_sections(status="000 0.0001\n", headers="", body=""))),
            ok_result(
                mngr_exec_json(
                    probe_sections(status="200 0.0041\n", headers="HTTP/1.1 200 OK\r\n", body="<h1>todo</h1>")
                )
            ),
        ],
    )

    _collector, environment = _flow_collector(tmp_path, agent, rules)

    opening = next(command for command in environment.exec_commands if "box_flow_step.py" in command)
    assert "https://todo-bb.{}.localhost:8431/".format(FAKE_WORKSPACE_AGENT_ID) in opening
    assert "gallery-aa" not in opening


def test_each_flow_drives_a_browser_of_its_own(tmp_path: Path) -> None:
    # Everything a flow needs to persist across its steps lives in the browser's default context,
    # which the browser process owns -- so the only thing that can keep one flow's cookies and
    # storage out of the next is a browser, and a profile, of its own.
    agent = ScriptedVerificationAgent(actions=[done_action(), done_action()], readings=[reading()])

    _collector, environment = _run_flow_collector(tmp_path, agent, flows=_TWO_FLOWS)

    launches = [command for command in environment.exec_commands if "--remote-debugging-port" in command]
    assert len(launches) == 2
    for flow_index, launch in enumerate(launches):
        assert "--remote-debugging-port={}".format(9333 + flow_index) in launch
        assert launch.startswith("profile=/tmp/minds-evals-chromium-{};".format(flow_index))
        # Whatever an earlier flow left running, or left on disk, goes first.
        assert "pkill -f" in launch and 'rm -rf "$profile"' in launch
    steps = [command for command in environment.exec_commands if "box_flow_step.py" in command]
    assert '"cdp_endpoint":"http://127.0.0.1:9334"' in steps[-1]


def test_a_step_whose_frame_was_not_captured_names_no_screenshot(tmp_path: Path) -> None:
    # The executor reports an empty screenshot path exactly when the capture failed, so naming the
    # file it would have written would point the grade-time judge at a frame that is not there.
    agent = ScriptedVerificationAgent(actions=[click_action(), done_action()], readings=[reading()])
    rules = _executor_rules()
    rules[4] = ScriptedExecRule("box_flow_step.py", [_step_result(), _step_result(screenshot_path="")])

    _collector, environment = _run_flow_collector(tmp_path, agent, rules)

    log = environment.uploaded_content_by_target["/logs/agent/verification/flows/add_complete_delete/log.jsonl"]
    # Two steps and the closing reading, none of which produced a frame.
    assert [json.loads(line)["screenshot"] for line in log.splitlines()] == ["", "", ""]


def test_collector_records_a_flow_that_ran_as_completed_whatever_the_app_showed(tmp_path: Path) -> None:
    # Trial time records that the declared steps were carried out; whether the app ended up in the
    # state the `expect` describes is the grade-time judge's call, from this evidence. Recording a
    # verdict here as well would be a second ruling on the same question, made with less to go on.
    agent = ScriptedVerificationAgent(actions=[done_action()], readings=[reading("the task never appeared")])

    collector, environment = _run_flow_collector(tmp_path, agent)

    entry = _flow_entries(collector)[0]
    assert (entry.status, entry.reason) == (CheckStatus.PASSED, "")
    # The agent's reading rides along as evidence, labelled as a reading rather than a verdict.
    assert "the task never appeared" in entry.detail
    assert "agent's reading of the final state" in entry.detail
    log = environment.uploaded_content_by_target["/logs/agent/verification/flows/add_complete_delete/log.jsonl"]
    last_record = json.loads(log.splitlines()[-1])
    assert last_record["action"] == "read the final state"
    assert last_record["reasoning"] == "the task never appeared"


def test_a_flow_that_ran_out_of_steps_is_incomplete(tmp_path: Path) -> None:
    # The one thing trial time does rule on: the flow never got to carry out what it declared, so
    # there is no final state for the judge to rule on either.
    agent = ScriptedVerificationAgent(actions=[click_action()], readings=[reading()])

    collector, _environment = _run_flow_collector(tmp_path, agent)

    entry = _flow_entries(collector)[0]
    assert (entry.status, entry.reason) == (CheckStatus.FAILED, ui_flows.REASON_STEP_BUDGET_EXHAUSTED)


def test_collector_keeps_going_when_an_action_does_not_land(tmp_path: Path) -> None:
    # An element that is not there is the app falling short, and the page below shows the truth --
    # so the flow records the failure where the judge will read it and carries on.
    agent = ScriptedVerificationAgent(
        actions=[click_action(), done_action()], readings=[reading("the final page does not list it")]
    )
    rules = _executor_rules()
    rules[4] = ScriptedExecRule(
        "box_flow_step.py",
        [
            _step_result(),
            _step_result(
                is_ok=False, reason=ui_flows.REASON_ACTION_TIMED_OUT, detail="locator resolved to 0 elements"
            ),
        ],
    )

    collector, environment = _run_flow_collector(tmp_path, agent, rules)

    entry = _flow_entries(collector)[0]
    # The flow still carried out its declared steps, so it completed; what the failed action means
    # for the `expect` is for the judge, which reads the error the log records below.
    assert entry.status is CheckStatus.PASSED
    log = environment.uploaded_content_by_target["/logs/agent/verification/flows/add_complete_delete/log.jsonl"]
    records = [json.loads(line) for line in log.splitlines()]
    assert any("locator resolved to 0 elements" in (record.get("error") or "") for record in records)


@pytest.mark.parametrize(
    "reason",
    [
        ui_flows.REASON_CDP_CONNECT_FAILED,
        ui_flows.REASON_FORWARD_UNREACHABLE,
        ui_flows.REASON_TUNNEL_DOWN,
        ui_flows.REASON_TLS_REFUSED,
        # An action kind the step script cannot perform, and an executor failure it could not name:
        # both are the harness, and neither is anything the workspace did.
        ui_flows.REASON_UNKNOWN_ACTION,
        ui_flows.REASON_STEP_ERROR,
    ],
)
def test_collector_records_each_executor_level_failure_as_its_own_error(tmp_path: Path, reason: str) -> None:
    # The executor is product machinery too, so when IT breaks the outcome must not read as "the
    # agent builds bad apps" -- and the reason has to name which layer went.
    # One acting step, so the failure lands on it rather than on the opening navigation.
    agent = ScriptedVerificationAgent(actions=[click_action(), done_action()], readings=[reading()])
    rules = _executor_rules()
    rules[4] = ScriptedExecRule(
        "box_flow_step.py", [_step_result(), _step_result(is_ok=False, reason=reason, detail="boom")]
    )

    collector, _environment = _run_flow_collector(tmp_path, agent, rules)

    entry = _flow_entries(collector)[0]
    assert (entry.status, entry.reason) == (CheckStatus.ERROR, reason)
    assert collector.manifest().is_evidence_complete is False


def test_collector_charges_a_timed_out_action_to_the_app(tmp_path: Path) -> None:
    # The other half of the same taxonomy: a page that never offered what the flow asked for is the
    # deliverable falling short, so it scores against the agent rather than being excluded.
    agent = ScriptedVerificationAgent(
        actions=[click_action(), done_action()], readings=[reading("the final page does not list it")]
    )
    rules = _executor_rules()
    rules[4] = ScriptedExecRule(
        "box_flow_step.py",
        [
            _step_result(is_ok=False, reason=ui_flows.REASON_ACTION_TIMED_OUT, detail="Timeout 15000ms exceeded"),
        ],
    )

    collector, _environment = _run_flow_collector(tmp_path, agent, rules)

    entry = _flow_entries(collector)[0]
    assert (entry.status, entry.reason) == (CheckStatus.FAILED, ui_flows.REASON_ACTION_TIMED_OUT)


def test_collector_treats_a_step_that_names_no_reason_as_an_executor_failure(tmp_path: Path) -> None:
    # A step reporting failure without naming a layer is the executor failing to say what happened.
    # Charging that to the agent would be the one thing the taxonomy exists to prevent.
    agent = ScriptedVerificationAgent(actions=[done_action()], readings=[reading()])
    rules = _executor_rules()
    rules[4] = ScriptedExecRule("box_flow_step.py", [_step_result(is_ok=False, reason="", detail="nothing said")])

    collector, _environment = _run_flow_collector(tmp_path, agent, rules)

    entry = _flow_entries(collector)[0]
    assert (entry.status, entry.reason) == (CheckStatus.ERROR, ui_flows.REASON_STEP_ERROR)


def test_collector_records_a_proxy_that_never_served_as_an_error(tmp_path: Path) -> None:
    # Readiness is a real request returning 200; a proxy that only ever 503s never served at all.
    agent = ScriptedVerificationAgent(actions=[done_action()], readings=[reading()])

    collector, _environment = _run_flow_collector(tmp_path, agent, _executor_rules(forward_probe="503"))

    entry = _flow_entries(collector)[0]
    assert (entry.status, entry.reason) == (CheckStatus.ERROR, ui_flows.REASON_FORWARD_UNREACHABLE)


def test_collector_records_a_browser_that_never_launched_as_an_error(tmp_path: Path) -> None:
    agent = ScriptedVerificationAgent(actions=[done_action()], readings=[reading()])

    collector, _environment = _run_flow_collector(tmp_path, agent, _executor_rules(is_browser_launched=False))

    entry = _flow_entries(collector)[0]
    assert (entry.status, entry.reason) == (CheckStatus.ERROR, ui_flows.REASON_BROWSER_LAUNCH_FAILED)


def test_collector_records_a_browser_that_never_took_cdp_as_an_error(tmp_path: Path) -> None:
    agent = ScriptedVerificationAgent(actions=[done_action()], readings=[reading()])

    collector, _environment = _run_flow_collector(tmp_path, agent, _executor_rules(browser_probe="not yet"))

    entry = _flow_entries(collector)[0]
    assert (entry.status, entry.reason) == (CheckStatus.ERROR, ui_flows.REASON_CDP_CONNECT_FAILED)


def test_collector_stops_a_flow_at_the_step_budget(tmp_path: Path) -> None:
    # An agent that never says "done" is looping, not progressing.
    agent = ScriptedVerificationAgent(actions=[click_action()], readings=[reading("the final page does not list it")])

    collector, _environment = _run_flow_collector(tmp_path, agent)

    assert agent.action_count == ui_flows.MAX_STEPS_PER_FLOW
    entry = _flow_entries(collector)[0]
    assert (entry.status, entry.reason) == (CheckStatus.FAILED, ui_flows.REASON_STEP_BUDGET_EXHAUSTED)


def test_collector_fails_a_flow_when_nothing_was_ever_served(tmp_path: Path) -> None:
    # No delivered app is the agent shipping nothing; the browser was never the problem.
    agent = ScriptedVerificationAgent(actions=[done_action()], readings=[reading()])
    preexisting_only = '[[apps]]\nname = "system_interface"\nurl = "http://localhost:8000"\n'

    collector, _environment = _flow_collector(
        tmp_path, agent, [*_executor_rules(), *_collector_rules(registry_text=preexisting_only)]
    )

    entry = _flow_entries(collector)[0]
    assert (entry.status, entry.reason) == (CheckStatus.FAILED, ui_flows.REASON_NO_APP_TO_OPEN)


def test_collector_records_an_unreadable_registry_as_an_error_not_a_missing_app(tmp_path: Path) -> None:
    # A registry we could not read says nothing about what was served -- that is the harness
    # failing to look, quite unlike a registry that lists nothing.
    agent = ScriptedVerificationAgent(actions=[done_action()], readings=[reading()])

    collector, _environment = _flow_collector(
        tmp_path, agent, [*_executor_rules(), *_collector_rules(registry_status="absent")]
    )

    entry = _flow_entries(collector)[0]
    assert entry.status is CheckStatus.ERROR


def test_collector_records_flows_as_unmeasured_without_a_preexisting_set(tmp_path: Path) -> None:
    agent = ScriptedVerificationAgent(actions=[done_action()], readings=[reading()])

    collector, _environment = _flow_collector(
        tmp_path,
        agent,
        [*_executor_rules(), *_collector_rules()],
        preexisting_registrations=None,
    )

    entry = _flow_entries(collector)[0]
    assert (entry.status, entry.reason) == (CheckStatus.ERROR, evidence_collection.REASON_PREEXISTING_UNKNOWN)


def test_collector_will_not_build_an_origin_from_an_unroutable_agent_id(tmp_path: Path) -> None:
    # The agent id is the origin coordinate; one the proxy does not route on would produce a URL it
    # silently declines rather than an error. Holding an unaddressable identity is the harness
    # losing track of the workspace, so it is an error -- the registry here lists a healthy app.
    agent = ScriptedVerificationAgent(actions=[done_action()], readings=[reading()])

    collector, environment = _flow_collector(
        tmp_path, agent, [*_executor_rules(), *_collector_rules()], agent_id="agent-72fdb075"
    )

    entry = _flow_entries(collector)[0]
    assert (entry.status, entry.reason) == (CheckStatus.ERROR, ui_flows.REASON_WORKSPACE_UNADDRESSABLE)
    assert not any("box_flow_step.py" in command for command in environment.exec_commands)


def test_collector_records_flows_as_unmeasurable_without_a_verification_agent(tmp_path: Path) -> None:
    collector, _environment = _flow_collector(tmp_path, None, [*_executor_rules(), *_collector_rules()])

    assert _flow_entries(collector)[0].reason == ui_flows.REASON_VERIFIER_AGENT_FAILED


def test_collector_stops_its_own_forward_instance_and_no_one_elses(tmp_path: Path) -> None:
    # The eval's instance is matched on the port it holds, so a forward the minds backend spawned
    # is never caught by the cleanup.
    agent = ScriptedVerificationAgent(actions=[done_action()], readings=[reading()])

    _collector, environment = _run_flow_collector(tmp_path, agent)

    stop = next(
        command for command in environment.exec_commands if "pkill -f" in command and "mngr forward" in command
    )
    assert "[-]-port 8431" in stop


def test_collector_screenshots_land_in_the_box_without_an_rsync(tmp_path: Path) -> None:
    # The browser runs in the box, so a frame is already where the artifact collector will find it.
    # There is no workspace staging leg at all, which the fleet executor needed and this does not.
    agent = ScriptedVerificationAgent(actions=[click_action(), done_action()], readings=[reading()])

    _collector, environment = _run_flow_collector(tmp_path, agent)

    step_commands = [command for command in environment.exec_commands if "box_flow_step.py" in command]
    assert any("/logs/agent/verification/flows/add_complete_delete/step_" in command for command in step_commands)
    assert not any("mngr rsync" in command and "flows" in command for command in environment.exec_commands)


def test_collector_reports_the_verification_agents_own_spend(tmp_path: Path) -> None:
    agent = ScriptedVerificationAgent(actions=[click_action(), done_action()], readings=[reading()])

    collector, _environment = _run_flow_collector(tmp_path, agent)

    usage = collector.verifier_usage()
    assert (usage.call_count, usage.model, usage.input_token_count) == (3, "claude-opus-4-8", 300)


def test_collector_drives_no_browser_when_the_case_declares_no_flows(tmp_path: Path) -> None:
    agent = ScriptedVerificationAgent(actions=[done_action()], readings=[reading()])

    collector, environment = _run_flow_collector(tmp_path, agent, flows=[])

    assert _flow_entries(collector) == []
    assert not any("box_flow_step.py" in command for command in environment.exec_commands)
    assert not any("mngr forward" in command for command in environment.exec_commands)
