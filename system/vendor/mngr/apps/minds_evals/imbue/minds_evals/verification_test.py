import asyncio
import json
import time
from pathlib import Path

import pytest

from imbue.minds_evals import verification
from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import CheckClass
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import Expectations
from imbue.minds_evals.data_types import HttpCheck
from imbue.minds_evals.data_types import RegisteredApp
from imbue.minds_evals.expectations import lower_expectations
from imbue.minds_evals.expectations import parse_expectations
from imbue.minds_evals.mock_environment_test import MockBoxEnvironment
from imbue.minds_evals.mock_environment_test import ScriptedExecRule
from imbue.minds_evals.mock_environment_test import failed_result
from imbue.minds_evals.mock_environment_test import mngr_exec_json
from imbue.minds_evals.mock_environment_test import ok_result

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
        expectations=lower_expectations(expectations) if expectations is not None else None,
        authored_expectations=expectations,
    )


def _sections(**named_bodies: str) -> str:
    return "".join("<<<MINDS_EVALS_SECTION:{}>>>\n{}".format(name, body) for name, body in named_bodies.items())


def _http_check(target: str = "registered-apps", expect_status: int = 200, expect_body_regex: str = "") -> HttpCheck:
    return HttpCheck(
        check_id="http_0", target=target, expect_status=expect_status, expect_body_regex=expect_body_regex
    )


# --- pure parsing helpers ---


def test_parse_apps_registry_reads_names_urls_and_marks_builtins() -> None:
    apps = verification.parse_apps_registry(_REGISTRY_TOML)

    assert apps is not None
    assert [(app.name, app.url, app.is_builtin) for app in apps] == [
        ("system_interface", "http://localhost:8000", True),
        ("todo", "http://localhost:8081", False),
    ]


@pytest.mark.parametrize("registry_text", ["not = [toml", 'apps = "wrong shape"'])
def test_parse_apps_registry_reports_an_unreadable_registry_as_none(registry_text: str) -> None:
    # None ("could not read it") and () ("read it; it lists nothing") are different claims: the
    # second is the agent shipping nothing, which must score against the agent.
    assert verification.parse_apps_registry(registry_text) is None


@pytest.mark.parametrize("registry_text", ["", "   ", "other = 1"])
def test_parse_apps_registry_reports_a_readable_but_empty_registry_as_no_apps(registry_text: str) -> None:
    assert verification.parse_apps_registry(registry_text) == ()


def test_parse_service_states_reads_program_states() -> None:
    assert verification.parse_service_states(_SERVICES_TEXT) == {
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
    assert verification.parse_service_states(services_text) == {}


def test_service_entries_find_a_grouped_supervisord_program() -> None:
    # supervisorctl prints a grouped program as `group:process`; a bare-name lookup alone would read
    # a perfectly healthy grouped service as absent and score it against the agent.
    delivered = (RegisteredApp(name="todo", url="http://localhost:8081", is_builtin=False, is_internal=False),)

    entries = verification.service_entries(
        "app_registered", delivered, {"apps:todo": "RUNNING"}, {"todo": "todo"}, True
    )

    assert entries[0].status is CheckStatus.PASSED


def test_split_sections_keeps_the_first_occurrence_of_a_marker() -> None:
    # An HTTP body and a test command's output are agent-controlled and are emitted after their
    # markers, so a later duplicate must never overwrite an earlier, harness-emitted section.
    forged = _sections(status="200 0.01\n", body="") + _sections(status="500 9.9\n")

    assert verification.split_sections(forged)["status"] == "200 0.01\n"


def test_split_sections_separates_one_commands_several_answers() -> None:
    assert verification.split_sections(
        "noise\n" + _sections(repo_root="/home/user/workspace\n", registry="x = 1\n", services="")
    ) == {"repo_root": "/home/user/workspace\n", "registry": "x = 1\n", "services": ""}


@pytest.mark.parametrize(
    ("status_section", "expected"),
    [("200 0.0041", (200, 0.004)), ("000 0.000153", (0, 0.0)), ("", (0, 0.0)), ("garbage", (0, 0.0))],
)
def test_parse_curl_status_reads_code_and_timing(status_section: str, expected: tuple[int, float]) -> None:
    assert verification.parse_curl_status(status_section) == expected


def test_http_entry_status_separates_a_broken_instrument_from_a_broken_app() -> None:
    # Not being able to ask means we could not find out (ERROR); an app answering wrong -- or
    # refusing the connection, which curl reports as 000 -- is the workspace falling short (FAILED).
    # The whole grading policy rests on that distinction.
    assert verification.http_entry_status(False, "", 200, "ok", _http_check()) == (
        CheckStatus.ERROR,
        verification.REASON_BRIDGE_FAILED,
    )
    assert verification.http_entry_status(True, "curl_missing", 0, "", _http_check()) == (
        CheckStatus.ERROR,
        verification.REASON_PROBE_UNAVAILABLE,
    )
    assert verification.http_entry_status(True, "", 500, "boom", _http_check()) == (
        CheckStatus.FAILED,
        verification.REASON_WRONG_STATUS,
    )
    assert verification.http_entry_status(True, "", 0, "", _http_check()) == (
        CheckStatus.FAILED,
        verification.REASON_WRONG_STATUS,
    )
    assert verification.http_entry_status(True, "", 200, "ok", _http_check()) == (CheckStatus.PASSED, "")


def test_http_entry_status_checks_a_declared_body_regex() -> None:
    check = _http_check(expect_body_regex="buy milk")

    assert verification.http_entry_status(True, "", 200, "<p>buy milk</p>", check) == (CheckStatus.PASSED, "")
    assert verification.http_entry_status(True, "", 200, "<p>nope</p>", check) == (
        CheckStatus.FAILED,
        verification.REASON_BODY_MISMATCH,
    )


def test_resolve_http_targets_fans_registered_apps_out_over_delivered_apps_only() -> None:
    apps = (
        RegisteredApp(name="terminal", url="http://localhost:7681", is_builtin=True, is_internal=False),
        RegisteredApp(name="todo", url="http://localhost:8081", is_builtin=False, is_internal=False),
        RegisteredApp(name="halfway", url="", is_builtin=False, is_internal=False),
    )

    delivered = verification.resolve_delivered_apps(apps, frozenset())
    assert [app.name for app in verification.resolve_http_targets(_http_check(), delivered)] == ["todo"]
    assert [app.name for app in verification.resolve_http_targets(_http_check(target="todo"), delivered)] == ["todo"]
    assert verification.resolve_http_targets(_http_check(target="absent"), delivered) == ()


def test_registration_entry_distinguishes_an_unreadable_registry_from_an_empty_one() -> None:
    delivered = (RegisteredApp(name="todo", url="http://localhost:8081", is_builtin=False, is_internal=False),)

    absent = verification.registration_entry("app_registered", 1, None, is_registry_present=False)
    unreadable = verification.registration_entry("app_registered", 1, None, is_registry_present=True)
    # An empty registry is the agent shipping nothing -- the failure this whole eval exists to catch
    # -- so it must score against the agent rather than error the trial.
    empty = verification.registration_entry("app_registered", 1, (), is_registry_present=True)

    assert (absent.status, absent.reason) == (CheckStatus.ERROR, verification.REASON_REGISTRY_ABSENT)
    assert (unreadable.status, unreadable.reason) == (CheckStatus.ERROR, verification.REASON_REGISTRY_UNREADABLE)
    assert (empty.status, empty.reason) == (CheckStatus.FAILED, verification.REASON_TOO_FEW_APPS)
    assert (
        verification.registration_entry("app_registered", 1, delivered, is_registry_present=True).status
        is CheckStatus.PASSED
    )
    assert (
        verification.registration_entry("app_registered", 2, delivered, is_registry_present=True).reason
        == verification.REASON_TOO_FEW_APPS
    )


def test_service_entries_flag_a_registered_app_whose_service_is_not_running() -> None:
    delivered = (RegisteredApp(name="todo", url="http://localhost:8081", is_builtin=False, is_internal=False),)

    programs = {"todo": "todo"}
    running = verification.service_entries("app_registered", delivered, {"todo": "RUNNING"}, programs, True)
    crashed = verification.service_entries("app_registered", delivered, {"todo": "FATAL"}, programs, True)
    unknown = verification.service_entries("app_registered", delivered, {}, programs, False)

    assert running[0].status is CheckStatus.PASSED
    assert crashed[0].status is CheckStatus.FAILED
    assert crashed[0].reason == verification.REASON_SERVICE_NOT_RUNNING
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

    assert verification.parse_supervised_registrations(conf) == {
        "system_interface": "system_interface",
        "shop": "dashboard",
        "shop-admin": "dashboard",
    }


def test_parse_supervised_registrations_accepts_either_flag_order() -> None:
    # The app scaffold writes --url first; the isolated-instance runner writes --name first.
    conf = "[program:todo]\ncommand=python3 system/scripts/forward_port.py --name todo --url http://localhost:8081\n"

    assert verification.parse_supervised_registrations(conf) == {"todo": "todo"}


def test_parse_isolated_instance_services_reads_every_concatenated_state_file() -> None:
    # The probe cats every instance.json, so the parser decodes one object at a time.
    instances = (
        json.dumps({"services": ["shop-preview", "si-preview"], "pids": [1, 2]})
        + "\n"
        + json.dumps({"services": ["scratch"]})
        + "\n"
    )

    assert verification.parse_isolated_instance_services(instances) == frozenset(
        {"shop-preview", "si-preview", "scratch"}
    )


@pytest.mark.parametrize("instances_text", ["", "   ", "not json at all"])
def test_parse_isolated_instance_services_tolerates_no_state(instances_text: str) -> None:
    assert verification.parse_isolated_instance_services(instances_text) == frozenset()


def test_parse_apps_registry_reads_the_internal_marker() -> None:
    # Verbatim shape from a live workspace: the owner-exec daemon forwards a port but has no page.
    registry = (
        '[[apps]]\nname = "owner-exec"\nurl = "http://127.0.0.1:8793"\nlabel = "owner-exec-75o5av89"\n'
        "internal = true\n\n"
        '[[apps]]\nname = "todo-list"\nurl = "http://localhost:8080"\nlabel = "todo-list-nyk8ptte"\n'
    )

    apps = verification.parse_apps_registry(registry)

    assert apps is not None
    assert [(app.name, app.is_internal) for app in apps] == [("owner-exec", True), ("todo-list", False)]


def test_resolve_delivered_apps_excludes_internal_machinery() -> None:
    # Regression from a live trial: owner-exec is registered but marked internal, and answers 404 on
    # its root by design. Counted as delivered it both inflated the app count and failed the implied
    # root-path probe -- charging the agent for a daemon it never shipped.
    apps = (
        RegisteredApp(name="owner-exec", url="http://127.0.0.1:8793", is_builtin=False, is_internal=True),
        RegisteredApp(name="todo-list", url="http://localhost:8080", is_builtin=False, is_internal=False),
    )

    delivered = verification.resolve_delivered_apps(apps, frozenset())

    assert [app.name for app in delivered] == ["todo-list"]


def test_service_entries_fall_back_to_a_program_named_like_the_row() -> None:
    # Covers a service that registers its port at runtime rather than through a forward_port call in
    # supervisord.conf, so the config join finds nothing but the program plainly exists.
    delivered = (RegisteredApp(name="todo-list", url="http://localhost:8080", is_builtin=False, is_internal=False),)

    entries = verification.service_entries("app_registered", delivered, {"todo-list": "RUNNING"}, {}, True)

    assert entries[0].status is CheckStatus.PASSED


def test_resolve_delivered_apps_excludes_abandoned_preview_rows() -> None:
    # A throwaway preview registers through the same path and leaves its row behind when abandoned.
    # Counting it would both satisfy app_registered on something that was never the deliverable and
    # fail the root-path probe on its dead port.
    apps = (
        RegisteredApp(name="terminal", url="http://localhost:7681", is_builtin=True, is_internal=False),
        RegisteredApp(name="shop", url="http://localhost:9000", is_builtin=False, is_internal=False),
        RegisteredApp(name="shop-preview", url="http://localhost:9100", is_builtin=False, is_internal=False),
    )

    delivered = verification.resolve_delivered_apps(apps, frozenset({"shop-preview"}))

    assert [app.name for app in delivered] == ["shop"]


def test_resolve_delivered_apps_keeps_a_real_app_whose_name_looks_like_a_preview() -> None:
    # Exclusion is by the instance runner's own record, not by name pattern: instance names are
    # caller-supplied, so a pattern would drop a genuine deliverable that happens to be named this
    # way and still miss throwaways named anything else.
    apps = (RegisteredApp(name="recipes-test", url="http://localhost:9000", is_builtin=False, is_internal=False),)

    assert [app.name for app in verification.resolve_delivered_apps(apps, frozenset())] == ["recipes-test"]


def test_service_entries_flag_a_registry_row_no_program_supervises() -> None:
    # An app started by hand and never wired into supervisord would not survive a restart, which is
    # a real shortfall of the minds-app contract -- recorded under its own reason so it stays
    # distinguishable from a program that exists and crashed.
    delivered = (RegisteredApp(name="handmade", url="http://localhost:9000", is_builtin=False, is_internal=False),)

    entries = verification.service_entries("app_registered", delivered, {"todo": "RUNNING"}, {"todo": "todo"}, True)

    assert entries[0].status is CheckStatus.FAILED
    assert entries[0].reason == verification.REASON_NO_SUPERVISED_PROGRAM


def test_service_entries_resolve_a_program_named_differently_from_the_row() -> None:
    delivered = (RegisteredApp(name="shop-admin", url="http://localhost:9001", is_builtin=False, is_internal=False),)

    entries = verification.service_entries(
        "app_registered", delivered, {"dashboard": "RUNNING"}, {"shop-admin": "dashboard"}, True
    )

    assert entries[0].status is CheckStatus.PASSED


def test_inventory_excludes_git_on_top_of_the_snapshot_excludes() -> None:
    # Loose objects would crowd real deliverable files out of the entry cap; the committed history
    # travels as the git bundle instead.
    assert ".git" in verification.INVENTORY_EXCLUDES
    assert "node_modules" in verification.INVENTORY_EXCLUDES


def test_test_command_wrapper_runs_the_command_in_a_subshell() -> None:
    # A declared test command ending in `exit` would otherwise take the probe down with it, losing
    # the exit code and output it exists to record.
    command = verification.test_command_wrapper("/home/user/workspace", "pytest -q")

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
    probe = _sections(
        repo_root="/home/user/workspace\n",
        registry_status=registry_status + "\n",
        registry=registry_text,
        services=services_text,
        supervisord=supervisord_conf,
        isolated_instances=isolated_instances,
    )
    repo_state = _sections(head_sha="b" * 40 + "\n", status="", commit_count="3\n", bundle="")
    http = _sections(status=http_status + "\n", headers="HTTP/1.1 200 OK\r\n", body="<h1>todo</h1>")
    test_result = _sections(exit_code=test_exit_code + "\n", output="1 passed\n")
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
) -> tuple[verification.EvidenceCollector, MockBoxEnvironment]:
    environment = MockBoxEnvironment(tmp_path, rules)
    logs_dir = tmp_path / "agent"
    logs_dir.mkdir(parents=True, exist_ok=True)
    collector = verification.EvidenceCollector(
        environment=environment,
        box_env={"MINDS_ENV": "staging"},
        workspace_agent_id="ws-1",
        case=case,
        clone_base_sha="a" * 40,
        dwt_tip_sha="e" * 40,
        host_logs_dir=logs_dir,
        deadline=time.monotonic() + deadline_offset_seconds,
    )
    asyncio.run(collector.collect(is_expectations_collection_wanted=is_expectations_collection_wanted))
    return collector, environment


def _entry_status_by_id(collector: verification.EvidenceCollector) -> dict[str, CheckStatus]:
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
    builtins_only = '[[apps]]\nname = "system_interface"\nurl = "http://localhost:8000"\n'

    collector, _environment = _run_collector(tmp_path, case, _collector_rules(registry_text=builtins_only))

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
    zero_commit_repo_state = _sections(head_sha="b" * 40 + "\n", status="", commit_count="0\n", bundle="no-commits")
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
    command = verification.repo_state_command("/home/user/workspace", "a" * 40)

    assert "no-commits" in command
    # A non-numeric or zero count short-circuits before `git bundle create` is ever reached.
    assert command.index("no-commits") < command.index("git bundle create")


def test_collector_records_a_failing_test_command_without_erroring(tmp_path: Path) -> None:
    case = _case_config(_authored(test_commands=["uv run pytest -q"]))

    collector, _environment = _run_collector(tmp_path, case, _collector_rules(test_exit_code="1"))

    failed = next(entry for entry in collector.entries if entry.entry_id == "test_command_0")
    assert failed.status is CheckStatus.FAILED
    assert failed.reason == verification.REASON_NONZERO_EXIT
    assert failed.check_class is CheckClass.TEST_COMMAND


def test_collector_records_timeouts_as_errors_rather_than_failures(tmp_path: Path) -> None:
    case = _case_config(_authored(test_commands=["uv run pytest -q"]))

    collector, _environment = _run_collector(tmp_path, case, _collector_rules(), deadline_offset_seconds=-1.0)

    statuses = _entry_status_by_id(collector)
    assert statuses["test_command_0"] is CheckStatus.ERROR
    assert statuses["http_0_registered_apps_todo"] is CheckStatus.ERROR
    timed_out = next(entry for entry in collector.entries if entry.entry_id == "test_command_0")
    assert timed_out.reason == verification.REASON_TIMEOUT


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

    files = verification.oracle_evidence_files(case)

    manifest = json.loads(files[verification.MANIFEST_FILENAME])
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
    assert json.loads(files[verification.REPO_STATE_FILENAME])["dwt_tip_sha"]
    registry_apps = verification.parse_apps_registry(files[verification.APPS_REGISTRY_FILENAME])
    assert registry_apps is not None
    assert [app.name for app in registry_apps if not app.is_builtin] == ["delivered-app"]
    assert "RUNNING" in files[verification.SERVICES_FILENAME]


def test_oracle_evidence_inventory_satisfies_declared_file_globs() -> None:
    case = _case_config(_authored(deliverable={"kind": "minds-app", "files": [{"glob": "workspace/apps/*/main.py"}]}))

    inventory = verification.oracle_evidence_files(case)[verification.FILE_INVENTORY_FILENAME]

    paths = [json.loads(line)["path"] for line in inventory.splitlines()]
    assert any(path.startswith("workspace/apps/") and path.endswith("main.py") for path in paths)
