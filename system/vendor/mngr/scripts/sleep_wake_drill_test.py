import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# scripts/sleep_wake_drill.py imports its sibling module bare (matching how it is
# invoked, `uv run --script scripts/sleep_wake_drill.py`). Make that resolvable
# for pytest by adding scripts/ to sys.path before importing it.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from imbue.minds.desktop_client.system_interface_health import AgentHealth  # noqa: E402
from scripts.sleep_wake_common import BlackHole  # noqa: E402
from scripts.sleep_wake_common import BlackHoleError  # noqa: E402
from scripts.sleep_wake_common import Clocks  # noqa: E402
from scripts.sleep_wake_common import EstablishedConnection  # noqa: E402
from scripts.sleep_wake_common import EventLog  # noqa: E402
from scripts.sleep_wake_common import IS_DARWIN  # noqa: E402
from scripts.sleep_wake_common import WakeObservation  # noqa: E402
from scripts.sleep_wake_common import build_black_hole_rules  # noqa: E402
from scripts.sleep_wake_common import parse_established_connections  # noqa: E402
from scripts.sleep_wake_drill import DrillError  # noqa: E402
from scripts.sleep_wake_drill import DrillTarget  # noqa: E402
from scripts.sleep_wake_drill import HostTarget  # noqa: E402
from scripts.sleep_wake_drill import LogWatcher  # noqa: E402
from scripts.sleep_wake_drill import MindsApp  # noqa: E402
from scripts.sleep_wake_drill import Sighting  # noqa: E402
from scripts.sleep_wake_drill import WakeSettler  # noqa: E402
from scripts.sleep_wake_drill import _LineAssembler  # noqa: E402
from scripts.sleep_wake_drill import _inode_or_none  # noqa: E402
from scripts.sleep_wake_drill import build_comparison  # noqa: E402
from scripts.sleep_wake_drill import build_post_wake_report  # noqa: E402
from scripts.sleep_wake_drill import build_report  # noqa: E402
from scripts.sleep_wake_drill import classify_record  # noqa: E402
from scripts.sleep_wake_drill import log_age_seconds  # noqa: E402
from scripts.sleep_wake_drill import parse_minds_apps  # noqa: E402
from scripts.sleep_wake_drill import resolve_app_mngr  # noqa: E402
from scripts.sleep_wake_drill import select_host  # noqa: E402
from scripts.sleep_wake_drill import workspace_query_for  # noqa: E402

_WORKSPACE_AGENT = "agent-11111111111111111111111111111111"
_SERVICES_AGENT = "agent-22222222222222222222222222222222"
_OTHER_AGENT = "agent-33333333333333333333333333333333"
_HOST = "host-44444444444444444444444444444444"

# The one marker whose message is not entirely literal: the tracker interpolates
# the prior state as ``AgentHealth.RECOVERING.value``, so the line reads lowercase
# where the neighbouring HEALTHY does not. Taken from the enum rather than retyped,
# so a change to the value fails here instead of quietly killing the marker.
_PROBE_RECOVERED_LINE = (
    f"System-interface health for {_WORKSPACE_AGENT}: {AgentHealth.RECOVERING.value} -> HEALTHY (probe succeeded)"
)


def _agent(
    agent_id: str,
    name: str,
    *,
    host_id: str = _HOST,
    host_name: str = "spare-box",
    display_name: str | None = None,
    has_ssh: bool = True,
) -> dict[str, Any]:
    host: dict[str, Any] = {"id": host_id, "name": host_name, "state": "RUNNING"}
    if has_ssh:
        host["ssh"] = {"user": "root", "host": "198.51.100.7", "port": 2222, "key_path": "/keys/id_ed25519"}
    labels = {"workspace_display_name": display_name} if display_name else {}
    return {"id": agent_id, "name": name, "labels": labels, "host": host}


def _wake(wall: float) -> WakeObservation:
    return WakeObservation(
        wake_clocks={"wall": wall, "py_monotonic": 100.0}, gaps={"wall": 300.0, "py_monotonic": 1.0}
    )


def _sighting(key: str, message: str, wall: float) -> Sighting:
    return Sighting(key=key, message=message, clocks={"wall": wall, "py_monotonic": 100.0})


def _app(data_dir: Path, bin_dir: Path) -> MindsApp:
    return MindsApp(
        pid=1234,
        data_dir=data_dir,
        log_file=data_dir / "logs" / "minds-events.jsonl",
        host_dir=data_dir / "mngr",
        mngr_command=[str(bin_dir / "mngr")],
    )


def _target() -> HostTarget:
    return HostTarget(
        host_id=_HOST,
        host_name="spare-box",
        display_name="Spare",
        ssh_host="198.51.100.7",
        ssh_port=2222,
        agent_ids=frozenset({_WORKSPACE_AGENT, _SERVICES_AGENT}),
        services_agent_id=_SERVICES_AGENT,
    )


# -- Finding the apps to drill ------------------------------------------------


# Both apps exactly as `ps -Awwo pid=,command=` renders them on a machine running
# a packaged app beside a dev one: a launcher process and, under it, the backend
# exec'd through its interpreter. Only the second of each pair is the app.
_PS_TWO_APPS = """\
  501 /Applications/Minds.app/Contents/Resources/uv/uv run --project /Applications/Minds.app/Contents/Resources/pyproject --active minds -v --format jsonl --log-file /Users/me/.minds/logs/minds-events.jsonl run --host 127.0.0.1 --port 60686 --no-browser
  502 /Users/me/.minds/.venv/bin/python3 /Users/me/.minds/.venv/bin/minds -v --format jsonl --log-file /Users/me/.minds/logs/minds-events.jsonl run --host 127.0.0.1 --port 60686 --no-browser
  503 uv run --package minds minds -vv --format jsonl --log-file /Users/me/.minds-staging/logs/minds-events.jsonl run --host 127.0.0.1 --port 61814 --no-browser
  504 /Users/me/checkout/.venv/bin/python3 /Users/me/checkout/.venv/bin/minds -vv --format jsonl --log-file /Users/me/.minds-staging/logs/minds-events.jsonl run --host 127.0.0.1 --port 61814 --no-browser
  505 /usr/bin/grep minds
  506 /Users/me/checkout/.venv/bin/python3 /Users/me/checkout/.venv/bin/minds --help
"""


def test_two_running_apps_are_found_with_the_paths_that_belong_to_each() -> None:
    """The whole point of the run: a build under test and a build from main, drilled together.

    They share nothing -- separate data dirs, separate logs, separate mngr
    installs -- so every path has to come from the app it belongs to. A console
    script is exec'd through its interpreter, so the entry point is the second
    token and not the first.
    """
    apps = parse_minds_apps(_PS_TWO_APPS)

    assert [app.label for app in apps] == [".minds", ".minds-staging"]
    installed, dev = apps
    assert installed.pid == 502
    assert installed.log_file == Path("/Users/me/.minds/logs/minds-events.jsonl")
    assert installed.host_dir == Path("/Users/me/.minds/mngr")
    assert installed.mngr_command == ["/Users/me/.minds/.venv/bin/mngr"]
    assert dev.pid == 504
    assert dev.host_dir == Path("/Users/me/.minds-staging/mngr")
    assert dev.mngr_command == ["/Users/me/checkout/.venv/bin/mngr"]


def test_the_launcher_above_each_backend_is_not_a_second_app() -> None:
    """Every app has one, carrying the same `run` and the same `--log-file`.

    Counting it would put each app in the comparison twice, and its bare `minds`
    argument has no virtualenv around it -- so the mngr "beside" it would be
    whichever is on PATH, which on this machine is another app's.
    """
    assert [app.pid for app in parse_minds_apps(_PS_TWO_APPS)] == [502, 504]


def test_a_minds_invocation_that_is_not_the_running_backend_is_not_an_app() -> None:
    """Only `minds ... run ... --log-file` is the backend writing the log this drill reads.

    Without both, the drill would adopt a short-lived CLI call or a grep for one
    as an app, then watch a log nothing writes -- and report that app as having
    ignored the outage.
    """
    not_backends = """\
  601 /usr/bin/grep minds
  602 /Users/me/checkout/.venv/bin/python3 /Users/me/checkout/.venv/bin/minds --help
  603 /Users/me/checkout/.venv/bin/python3 /Users/me/checkout/.venv/bin/minds ls --format json
  604 /Users/me/checkout/.venv/bin/python3 /Users/me/checkout/.venv/bin/minds run --host 127.0.0.1
"""

    assert parse_minds_apps(not_backends) == []


def test_an_app_logging_somewhere_unusual_is_still_followed() -> None:
    """The log is the one the app was told to write, not the one its data dir implies."""
    line = "  777 /opt/py/bin/python3 /opt/minds/bin/minds --log-file /var/log/minds/events.jsonl run\n"

    (app,) = parse_minds_apps(line)

    assert app.log_file == Path("/var/log/minds/events.jsonl")
    assert app.mngr_command == ["/opt/minds/bin/mngr"]


# -- Resolving the machine the run is about to black-hole ---------------------


@pytest.mark.parametrize(
    "query", [_WORKSPACE_AGENT, "quiet-amber-heron", "Spare workspace", "spare-box", "SPARE WORKSPACE"]
)
def test_a_machine_resolves_from_any_handle_of_any_agent_on_it(query: str) -> None:
    """Every name a user would reach for names the same machine.

    The display name is what the app shows, the host and agent names are what
    mngr shows, and the agent id is what the log carries -- and the run has to
    pick the same machine whichever one is typed, because it is about to take
    that machine off the network.
    """
    agents = [
        _agent(_WORKSPACE_AGENT, "quiet-amber-heron", display_name="Spare workspace"),
        _agent(_SERVICES_AGENT, "system-services"),
    ]

    target = select_host(agents, query)

    assert target.host_id == _HOST
    assert target.ssh_host == "198.51.100.7"
    assert target.ssh_port == 2222
    assert target.agent_ids == frozenset({_WORKSPACE_AGENT, _SERVICES_AGENT})
    assert target.services_agent_id == _SERVICES_AGENT


def test_the_machine_minds_calls_a_workspace_by_its_services_agent_is_still_drillable() -> None:
    """The shape of a plain minds laptop, and what the run used to refuse outright.

    A machine there has one agent, minds treats it as the workspace, and it is
    called ``system-services``. Excluding that name -- which made sense while a
    run had to pick one agent among several chats -- left nothing to pick, so
    the drill reported the app as having no workspace to drill.
    """
    agents = [_agent(_SERVICES_AGENT, "system-services", display_name="workspace-1")]

    target = select_host(agents, None)

    assert target.agent_ids == frozenset({_SERVICES_AGENT})
    assert target.services_agent_id == _SERVICES_AGENT


def test_a_machine_with_several_agents_watches_all_of_them() -> None:
    """One address serves every agent on the machine, so the outage is the same for all of them.

    Which one the app enrols, convicts and recovers is not knowable in advance,
    and does not need to be.
    """
    agents = [
        _agent(_WORKSPACE_AGENT, "Chat-1"),
        _agent(_OTHER_AGENT, "Chat-2"),
        _agent(_SERVICES_AGENT, "system-services"),
    ]

    target = select_host(agents, "Chat-2")

    assert target.agent_ids == frozenset({_WORKSPACE_AGENT, _OTHER_AGENT, _SERVICES_AGENT})


def test_the_services_agent_is_taken_from_the_same_machine_only() -> None:
    """A second machine's system-services agent is not this machine's, and does not stand in for it.

    Both are named ``system-services``; only the host id tells them apart, and
    naming the wrong one would attribute another machine's recovery to this run.
    """
    agents = [
        _agent(_WORKSPACE_AGENT, "quiet-amber-heron", display_name="Spare workspace"),
        _agent(_SERVICES_AGENT, "system-services", host_id="host-elsewhere", host_name="other-box"),
    ]

    with pytest.raises(DrillError, match="no system-services agent"):
        select_host(agents, "Spare workspace")


def test_a_machine_whose_agents_discovery_has_not_named_says_so() -> None:
    """Before discovery names them, every agent's name is its own id -- including the services one.

    Drilling then would watch for markers about agents the app may rename under
    the run, so the refusal points at the cause rather than at the machine.
    """
    agents = [_agent(_WORKSPACE_AGENT, _WORKSPACE_AGENT)]

    with pytest.raises(DrillError, match="discovery has not named them yet"):
        select_host(agents, None)


def test_an_app_with_one_running_machine_is_drilled_without_being_told_which() -> None:
    """The ordinary case for a drill laptop, and the reason nothing has to be passed."""
    agents = [
        _agent(_WORKSPACE_AGENT, "quiet-amber-heron", display_name="Spare"),
        _agent(_SERVICES_AGENT, "system-services"),
    ]

    assert select_host(agents, None).host_id == _HOST


def test_an_app_with_several_running_machines_refuses_to_pick_one_unasked() -> None:
    """Auto-selection stops where it would be a guess: this run takes a machine off the network."""
    agents = [
        _agent(_WORKSPACE_AGENT, "quiet-amber-heron", display_name="Spare"),
        _agent(_SERVICES_AGENT, "system-services"),
        _agent(_OTHER_AGENT, "system-services", host_id="host-elsewhere", host_name="other-box"),
    ]

    with pytest.raises(DrillError, match="several running machines.*--workspace"):
        select_host(agents, None)


def test_an_ambiguous_name_refuses_to_pick_one() -> None:
    """Two machines sharing a display name is a stop, not a coin flip."""
    agents = [
        _agent(_WORKSPACE_AGENT, "system-services", display_name="Spare"),
        _agent(_OTHER_AGENT, "system-services", host_id="host-elsewhere", host_name="other-box", display_name="Spare"),
    ]

    with pytest.raises(DrillError, match="matches several machines"):
        select_host(agents, "Spare")


def test_a_machine_with_no_ssh_endpoint_is_not_a_candidate() -> None:
    """There is nothing to black-hole for a local or offline machine."""
    agents = [_agent(_SERVICES_AGENT, "system-services", display_name="Spare", has_ssh=False)]

    with pytest.raises(DrillError, match="no running remote machine"):
        select_host(agents, None)


def test_each_app_can_be_told_which_of_its_machines_to_drill() -> None:
    """The apps being compared do not share machines, so one query cannot resolve in both.

    A build under test and a build from main each keep their own workspace on
    their own box. A single query would match in one app and match nothing in
    the other, which reads as that machine being gone rather than as the query
    being for someone else.
    """
    specs = [".minds=geebspace", ".minds-staging=workspace-1"]

    assert workspace_query_for(specs, ".minds") == "geebspace"
    assert workspace_query_for(specs, ".minds-staging") == "workspace-1"


def test_an_unaddressed_workspace_applies_to_every_app() -> None:
    """One machine name across both apps is the common case, and needs no prefix."""
    assert workspace_query_for(["spare-box"], ".minds") == "spare-box"
    assert workspace_query_for(["spare-box"], ".minds-staging") == "spare-box"
    assert workspace_query_for([".minds=geebspace", "spare-box"], ".minds-staging") == "spare-box"


def test_nothing_passed_leaves_each_app_to_its_only_machine() -> None:
    """The ordinary case, and the reason the drill needs no arguments at all."""
    assert workspace_query_for(None, ".minds") is None
    assert workspace_query_for([], ".minds") is None


def test_two_unaddressed_workspaces_refuse_to_guess_which_app_each_is_for() -> None:
    """Both would apply to every app, so neither can be the one meant."""
    with pytest.raises(DrillError, match="not addressed to an app"):
        workspace_query_for(["geebspace", "workspace-1"], ".minds")


# -- Reading minds' log -------------------------------------------------------


def test_the_stuck_edge_is_recognised_for_this_agent_and_no_other() -> None:
    """A neighbouring workspace's outage must not satisfy this run's first step.

    A drill machine usually has several workspaces, all of them logging the same
    sentences about different agents.
    """
    stuck = {
        "module": "system_interface_health",
        "message": f"System-interface health for {_WORKSPACE_AGENT}: HEALTHY -> STUCK after 6.0s of "
        "continuous probe failures",
    }
    neighbour = dict(stuck, message=stuck["message"].replace(_WORKSPACE_AGENT, _OTHER_AGENT))

    classified = classify_record(stuck, frozenset({_WORKSPACE_AGENT}))
    assert classified is not None
    marker, agent_id = classified
    assert marker.key == "stuck"
    assert agent_id == _WORKSPACE_AGENT
    assert classify_record(neighbour, frozenset({_WORKSPACE_AGENT})) is None


def test_a_record_read_while_it_was_still_being_written_is_not_lost() -> None:
    """The follower reads a file another process appends to, so a record can arrive in pieces.

    Handing each piece straight on drops the record -- neither parses -- and the
    marker it carried never reaches the report, which then says a step did not
    hold when it did.
    """
    assembler = _LineAssembler()
    record = json.dumps({"module": "system_interface_health", "message": "HEALTHY -> STUCK"})

    assert assembler.feed(record[:20]) == []
    assert assembler.feed(record[20:]) == []
    assert assembler.feed("\n") == [record]


def test_only_a_newline_ends_a_record() -> None:
    """A record is whatever lies between newlines, whichever other line breaks it carries.

    Python counts several characters besides ``\\n`` as line boundaries, and a
    record cut on one of them arrives as two fragments that do not parse -- the
    same loss as a torn read, reached from the other direction.
    """
    assembler = _LineAssembler()
    # U+2028 LINE SEPARATOR: a line boundary to str.splitlines, not to the reader.
    record = json.dumps({"module": "system_interface_health", "message": "one\u2028two"}, ensure_ascii=False)

    assert assembler.feed(record + "\n") == [record]


def test_the_gap_a_rotation_opens_is_not_an_error(tmp_path: Path) -> None:
    """minds' sink rotates by renaming and reopening, so the path the follower watches is briefly absent.

    A raising stat there escapes the follower thread and takes every later
    marker of the run with it, leaving a report that says a step did not hold
    with only a stray traceback to say why.
    """
    log_file = tmp_path / "minds-events.jsonl"
    log_file.write_text("{}\n")

    assert _inode_or_none(log_file) == log_file.stat().st_ino

    log_file.rename(tmp_path / "minds-events.1.jsonl")

    assert _inode_or_none(log_file) is None


def test_the_remainder_of_a_rotated_file_is_not_glued_onto_the_next_one() -> None:
    """A reset drops what belongs to the handle being closed, which is not a prefix of the new file."""
    assembler = _LineAssembler()

    assembler.feed('{"module": "system_int')
    assembler.reset()

    assert assembler.feed('{"module": "workspace_recovery"}\n') == ['{"module": "workspace_recovery"}']


def test_a_line_quoting_a_marker_from_another_module_does_not_count() -> None:
    """The module gates the match, so a subprocess line echoing minds' own text is not a state change.

    ``forward_cli`` relays the mngr forward's stderr into this same log, which
    is where a quoted-back sentence would come from.
    """
    relayed = {
        "module": "forward_cli",
        "message": f"mngr forward stderr: | System-interface health for {_WORKSPACE_AGENT}: HEALTHY -> STUCK",
    }

    assert classify_record(relayed, frozenset({_WORKSPACE_AGENT})) is None


def test_every_step_of_the_chain_has_a_marker_that_matches_its_log_line() -> None:
    """The messages the drill waits for are the ones the code actually writes.

    Each string below is copied from the emitting call site; if one is
    reworded and the marker is not, the drill silently reports UNMEASURED for a
    step that ran.
    """
    lines = {
        "dispatched": ("workspace_recovery", f"Unattended recovery for {_WORKSPACE_AGENT}: DISPATCHED"),
        "start_only": ("workspace_recovery", f"Start-only recovery for {_WORKSPACE_AGENT}: skipping the stop step"),
        "invalidated": (
            "system_interface_health",
            f"Recovery of {_WORKSPACE_AGENT} was in flight across a sleep; probing it again rather than waiting on it",
        ),
        "probe_recovered": ("system_interface_health", _PROBE_RECOVERED_LINE),
        "failure_declined": (
            "system_interface_health",
            f"Recovery failure for {_WORKSPACE_AGENT} not shown (Start step of host recovery failed): a probe found "
            "the machine answering while it was still running",
        ),
        "step_failed": (
            "workspace_recovery",
            f"Start step of host recovery for {_WORKSPACE_AGENT} failed (reported as a failed step): timed out",
        ),
    }

    for key, (module, message) in lines.items():
        classified = classify_record({"module": module, "message": message}, frozenset({_WORKSPACE_AGENT}))
        assert classified is not None, f"no marker matched the {key} line"
        assert classified[0].key == key


# -- The report ---------------------------------------------------------------


def test_a_start_that_completed_after_the_wake_reports_the_whole_chain_holding() -> None:
    """The expected ending: mngr unpins the start itself and no failure is ever raised.

    Step 5 has to read HOLDS here without a declined failure to point at,
    because there was no failure -- and it has to say that is why, so the run is
    not read as having exercised the decline path.
    """
    sightings = [
        _sighting("stuck", f"System-interface health for {_WORKSPACE_AGENT}: HEALTHY -> STUCK after 6.0s", 1000.0),
        _sighting("dispatched", f"Unattended recovery for {_WORKSPACE_AGENT}: DISPATCHED", 1001.0),
        _sighting("start_only", f"Start-only recovery for {_WORKSPACE_AGENT}: skipping the stop step", 1002.0),
        _sighting("invalidated", f"Recovery of {_WORKSPACE_AGENT} was in flight across a sleep", 1301.0),
        _sighting("probe_recovered", _PROBE_RECOVERED_LINE, 1304.0),
    ]

    report = "\n".join(
        build_report(
            app_label="minds",
            target=_target(),
            sightings=sightings,
            mentions=12,
            wakes=[_wake(1300.0)],
            black_hole_installed_wall=900.0,
            black_hole_removed_wall=1300.0,
        )
    )

    assert "DOES NOT HOLD" not in report
    assert "UNMEASURED" not in report
    assert "convicted after 6.0s" in report
    assert "+4.0s after the wake" in report
    assert "the start never reported a failure" in report


def test_a_failure_the_probe_outranked_is_reported_as_the_backstop_working() -> None:
    """The backstop ending: the start errored, but a probe had already answered for the machine."""
    sightings = [
        _sighting("stuck", f"System-interface health for {_WORKSPACE_AGENT}: HEALTHY -> STUCK after 6.0s", 1000.0),
        _sighting("dispatched", f"Unattended recovery for {_WORKSPACE_AGENT}: DISPATCHED", 1001.0),
        _sighting("start_only", f"Start-only recovery for {_WORKSPACE_AGENT}: skipping the stop step", 1002.0),
        _sighting("invalidated", f"Recovery of {_WORKSPACE_AGENT} was in flight across a sleep", 1301.0),
        _sighting("probe_recovered", _PROBE_RECOVERED_LINE, 1304.0),
        _sighting("step_failed", f"Start step of host recovery for {_WORKSPACE_AGENT} failed", 1400.0),
        _sighting("failure_declined", f"Recovery failure for {_WORKSPACE_AGENT} not shown", 1400.0),
    ]

    report = "\n".join(
        build_report(
            app_label="minds",
            target=_target(),
            sightings=sightings,
            mentions=12,
            wakes=[_wake(1300.0)],
            black_hole_installed_wall=900.0,
            black_hole_removed_wall=1300.0,
        )
    )

    assert "DOES NOT HOLD" not in report
    assert "Start step of host recovery" in report
    assert "carrying the error as a caveat" in report


def test_a_failure_that_reached_the_card_is_reported_as_the_regression_it_is() -> None:
    """The same failure without the decline is the bug this branch exists to prevent."""
    sightings = [
        _sighting("stuck", f"System-interface health for {_WORKSPACE_AGENT}: HEALTHY -> STUCK after 6.0s", 1000.0),
        _sighting("dispatched", f"Unattended recovery for {_WORKSPACE_AGENT}: DISPATCHED", 1001.0),
        _sighting("start_only", f"Start-only recovery for {_WORKSPACE_AGENT}: skipping the stop step", 1002.0),
        _sighting("invalidated", f"Recovery of {_WORKSPACE_AGENT} was in flight across a sleep", 1301.0),
        _sighting("probe_recovered", _PROBE_RECOVERED_LINE, 1304.0),
        _sighting(
            "recovery_failed", f"Host recovery of {_WORKSPACE_AGENT} failed: the interface did not respond", 1400.0
        ),
    ]

    report = "\n".join(
        build_report(
            app_label="minds",
            target=_target(),
            sightings=sightings,
            mentions=12,
            wakes=[_wake(1300.0)],
            black_hole_installed_wall=900.0,
            black_hole_removed_wall=1300.0,
        )
    )

    assert "5. No recovery-failed card was raised." in report
    assert "The failure was NOT declined" in report
    assert "DOES NOT HOLD" in report


def test_a_run_that_never_dispatched_a_start_does_not_blame_the_wake_handling() -> None:
    """With nothing in flight, the wake had nothing to hand back, so step 3 is unmeasured rather than failed.

    This is the shape of a run where the black hole caught the connectivity
    quorum and the app withheld the start as owed -- a staging failure, and it
    must not read as a defect in the code under test.
    """
    sightings = [
        _sighting("stuck", f"System-interface health for {_WORKSPACE_AGENT}: HEALTHY -> STUCK after 6.0s", 1000.0)
    ]

    report = "\n".join(
        build_report(
            app_label="minds",
            target=_target(),
            sightings=sightings,
            mentions=12,
            wakes=[_wake(1300.0)],
            black_hole_installed_wall=900.0,
            black_hole_removed_wall=1300.0,
        )
    )

    step_three = report.split("3. The wake handed")[1].split("4. A probe")[0]
    assert "verdict: UNMEASURED" in step_three
    assert "Nothing to mark: no START was in flight" in step_three


def test_a_dispatch_that_lost_the_operation_slot_does_not_blame_the_wake_handling_either() -> None:
    """The app logs a line for every dispatch outcome, and only one of them spawns a worker.

    A restore stops the workspace's services for minutes, which is what drives
    the agent STUCK in the first place -- so the unattended dispatch losing the
    single operation slot to it is the ordinary way a staged run ends with
    nothing in flight. Reading that as the wake having failed to mark a start
    would convict the code under test for a run that never staged one.
    """
    sightings = [
        _sighting("stuck", f"System-interface health for {_WORKSPACE_AGENT}: HEALTHY -> STUCK after 6.0s", 1000.0),
        _sighting("dispatched", f"Unattended recovery for {_WORKSPACE_AGENT}: OPERATION_CONFLICT", 1001.0),
    ]

    report = "\n".join(
        build_report(
            app_label="minds",
            target=_target(),
            sightings=sightings,
            mentions=12,
            wakes=[_wake(1300.0)],
            black_hole_installed_wall=900.0,
            black_hole_removed_wall=1300.0,
        )
    )

    step_two = report.split("2. The unattended start")[1].split("3. The wake handed")[0]
    step_three = report.split("3. The wake handed")[1].split("4. A probe")[0]
    assert "verdict: DOES NOT HOLD" in step_two
    # The missing start-only marker is not evidence of a RESTART: nothing ran.
    assert "No worker was spawned for this outcome" in step_two
    assert "RESTART" not in step_two
    assert "verdict: UNMEASURED" in step_three
    assert "Nothing to mark: no START was in flight" in step_three


# -- The post-wake scenario ---------------------------------------------------


_KILLED_TUNNEL = EstablishedConnection(local_port=51000, peer_ip="198.51.100.7", peer_port=2222, process="minds:502")


def _post_wake(
    sightings: list[Sighting],
    *,
    mentions: int = 12,
    blocked_connections: list[EstablishedConnection] | None = None,
) -> list[str]:
    return build_post_wake_report(
        app_label="minds",
        target=_target(),
        sightings=sightings,
        mentions=mentions,
        wakes=[_wake(1000.0)],
        black_hole_installed_wall=1001.0,
        black_hole_removed_wall=1301.0,
        blocked_connections=[_KILLED_TUNNEL] if blocked_connections is None else blocked_connections,
    )


def _verdict_under(report: list[str], heading_prefix: str) -> str:
    """The verdict line of the numbered step whose heading starts with ``heading_prefix``."""
    start = next(i for i, line in enumerate(report) if line.startswith(heading_prefix))
    return next(line.strip() for line in report[start:] if line.strip().startswith("verdict:"))


def test_a_wake_the_app_sat_through_quietly_is_the_fix_holding() -> None:
    """A build that retires its tunnels at the wake never sends a byte on a dead connection.

    So its log carries no failed request at all, and that silence has to read as
    the fix rather than as an outage that never happened: what says the outage
    was real is the connection the drill found open and killed, not the app.
    """
    report = _post_wake([])

    assert "1 dead from the wake (minds:502 :51000->2222)" in "\n".join(report)
    assert "tunnels were rebuilt" in "\n".join(report)
    assert _verdict_under(report, "0.") == "verdict: HOLDS"
    assert _verdict_under(report, "2.") == "verdict: HOLDS"
    assert _verdict_under(report, "3.") == "verdict: HOLDS"


def test_a_build_that_handed_the_old_tunnel_back_but_did_not_convict_is_also_holding() -> None:
    """Failed requests without a conviction are the grace doing its work, not a defect."""
    report = _post_wake(
        [
            _sighting("connection_failure", "System-interface connection failure for agent: CONNECT_ERROR", 1031.0),
            _sighting("enrolled", "Enrolled agent as a system-interface probe suspect", 1031.0),
        ]
    )

    assert "+31.0s after the wake" in "\n".join(report)
    assert _verdict_under(report, "2.") == "verdict: HOLDS"
    assert _verdict_under(report, "3.") == "verdict: HOLDS"


def test_an_app_with_nothing_open_at_the_wake_is_not_a_fence_holding() -> None:
    """No connection killed means no outage staged for that app, whatever its log says.

    Its silence would otherwise read as the strongest possible pass, when the
    rule never named a single one of its connections.
    """
    report = _post_wake([], blocked_connections=[])

    assert "nothing was open to this machine at the wake" in "\n".join(report)
    assert _verdict_under(report, "0.") == "verdict: DOES NOT HOLD"
    assert _verdict_under(report, "2.") == "verdict: UNMEASURED"
    assert _verdict_under(report, "3.") == "verdict: UNMEASURED"


def test_a_conviction_inside_the_window_is_reported_as_the_defect() -> None:
    """Convicting is the failure here, which is the opposite of the recovery scenario.

    The report has to say so plainly, and say that the restart it dispatched went
    to a machine that was running.
    """
    report = "\n".join(
        _post_wake(
            [
                _sighting(
                    "connection_failure", "System-interface connection failure for agent: CONNECT_ERROR", 1031.0
                ),
                _sighting("stuck", "HEALTHY -> STUCK after 8.0s of continuous probe failures", 1033.0),
                _sighting("dispatched", "Unattended recovery for agent: DISPATCHED", 1034.0),
            ]
        )
    )

    assert "+33.0s after the wake" in report
    assert "restarted a machine that was fine" in report
    assert report.count("DOES NOT HOLD") == 2


def test_an_app_nothing_was_looking_at_is_not_counted_as_holding() -> None:
    """Silence from a workspace no window was open on measures nothing at all.

    Without this the run's most common setup mistake reads as the strongest
    possible pass: no conviction, no dispatch, everything green.
    """
    report = _post_wake([], mentions=0)

    assert "Open a window on it and re-run." in "\n".join(report)
    assert _verdict_under(report, "1.") == "verdict: DOES NOT HOLD"
    assert _verdict_under(report, "2.") == "verdict: UNMEASURED"
    assert _verdict_under(report, "3.") == "verdict: UNMEASURED"


# -- The one table the run exists for -----------------------------------------


def _drill_target(tmp_path: Path, label: str, sightings: list[Sighting]) -> DrillTarget:
    data_dir = tmp_path / label
    (data_dir / "logs").mkdir(parents=True)
    log = EventLog(tmp_path / f"{label}-drill.jsonl", Clocks())
    watcher = LogWatcher(
        path=data_dir / "logs" / "minds-events.jsonl",
        agent_ids=frozenset({_WORKSPACE_AGENT}),
        log=log,
        clocks=Clocks(),
    )
    watcher.sightings.extend(sightings)
    return DrillTarget(
        app=_app(data_dir, tmp_path / "bin"),
        host=_target(),
        peer_ip="198.51.100.7",
        watcher=watcher,
    )


def test_the_comparison_reads_the_regression_and_the_fix_off_the_same_wake(tmp_path: Path) -> None:
    """The deliverable: two apps, one outage, one wake, and what each of them did with it.

    The app that convicted before the sleep and came back after the wake with no
    card is the fixed one; the app that ended on a recovery-failed card is the
    incident. Both are read against the same wake, which is the only instant the
    two apps share.
    """
    fixed = _drill_target(
        tmp_path,
        "fixed",
        [
            _sighting("stuck", "STUCK after 8.0s", 900.0),
            _sighting("dispatched", "Unattended recovery: DISPATCHED", 901.0),
            _sighting("invalidated", "was in flight across a sleep", 1002.0),
            _sighting("probe_recovered", _PROBE_RECOVERED_LINE, 1005.0),
        ],
    )
    broken = _drill_target(
        tmp_path,
        "broken",
        [
            _sighting("stuck", "STUCK after 8.0s", 900.0),
            _sighting("dispatched", "Unattended recovery: DISPATCHED", 901.0),
            _sighting("recovery_failed", "Host recovery failed", 1100.0),
        ],
    )

    lines = build_comparison([fixed, broken], [_wake(1000.0)])
    rows = {line.split()[0]: line for line in lines if line.startswith(("fixed", "broken"))}

    # Convicted 100s before the wake, handed back 2s after it, answering at 5s.
    assert "-100s" in rows["fixed"] and "+2s" in rows["fixed"] and "+5s" in rows["fixed"]
    assert rows["fixed"].endswith("none")
    # The regression: no handoff at the wake, no probe success, and a card.
    assert rows["broken"].endswith("RECOVERY_FAILED")


def test_an_app_that_never_reached_a_step_leaves_that_column_empty(tmp_path: Path) -> None:
    """A step that never happened is not a step that happened at time zero."""
    target = _drill_target(tmp_path, "quiet", [])

    (row,) = [line for line in build_comparison([target], [_wake(1000.0)]) if line.startswith("quiet")]

    assert row.split() == ["quiet", "-", "-", "-", "-", "none"]


def test_a_run_with_no_wake_says_so_rather_than_timing_against_nothing(tmp_path: Path) -> None:
    """Without a wake there is no shared instant, so no time in this table would mean anything."""
    target = _drill_target(tmp_path, "solo", [_sighting("stuck", "STUCK after 8.0s", 900.0)])

    (row,) = [line for line in build_comparison([target], []) if line.startswith("solo")]

    assert "no wake" in row


# -- The fault injection itself -----------------------------------------------


def test_the_post_wake_rule_names_the_connections_it_kills_and_leaves_the_address_reachable() -> None:
    """Scoping is the difference between the incident and a different outage.

    The incident's network killed the connections that predated the sleep and
    nothing else: a fresh connection to the same address went through. A rule
    on the whole address stages a machine that is unreachable, which both builds
    are right to convict, so it belongs to the recovery scenario alone.
    """
    post_wake_rules = build_black_hole_rules(["198.51.100.7"], peer_ports=None, local_ports=[51000, 51001])
    recovery_rules = build_black_hole_rules(["198.51.100.7"], peer_ports=None, local_ports=None)

    assert "block drop out quick proto tcp from any port { 51000, 51001 } to 198.51.100.7\n" in post_wake_rules
    assert "block drop in quick proto tcp from 198.51.100.7 to any port { 51000, 51001 }\n" in post_wake_rules
    assert "block drop out quick proto tcp from any to 198.51.100.7\n" in recovery_rules
    assert "block drop in quick proto tcp from 198.51.100.7 to any\n" in recovery_rules


# `netstat -anv -p tcp` on macOS: one connection to each of two boxes, a second
# connection to the first box from another process, a listener, a half-closed
# connection to the first box, and a link-local IPv6 connection whose address
# the column width truncates.
_NETSTAT_AT_THE_WAKE = """\
Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address                                 Foreign Address                               (state)          rxbytes      txbytes  rhiwat  shiwat          process:pid    state  options           gencnt    flags   flags1 usecnt rtncnt fltrs
tcp4       0      0  10.0.0.5.51000         198.51.100.7.2222      ESTABLISHED         7878         3036  131072  131768       python3.12:502  00102 00000000 0000000001392995 00000080 04000800      2      0 000000
tcp4       0      0  10.0.0.5.51001         203.0.113.9.2211       ESTABLISHED         5052        19104  131072 4194304       python3.12:504  00102 00000000 0000000001392996 00000080 04000800      2      0 000000
tcp4       0      0  10.0.0.5.51002         198.51.100.7.22        ESTABLISHED          416          545  408192  146988             ssh:777  00102 00000000 0000000001392997 00000080 04000800      2      0 000000
tcp4       0      0  127.0.0.1.55636        *.*                    LISTEN                 0            0  131072  131072 sculptor_backend:83111  00100 00000006 0000000001385293 00000000 00000800      1      0 000000
tcp4       0      0  10.0.0.5.51003         198.51.100.7.2222      CLOSE_WAIT          9464         2310  131072  131600       python3.12:502  00122 00000000 0000000001386b99 00000080 04000800      2      0 000000
tcp6       0      0  fe80::7811:daff:.64702 fe80::c8f5:4fff:.53009 ESTABLISHED         5052        19104  131072 4194304         rapportd:639    00000 00000000 0000000000000000 00000000 00000000      0      0 000000
"""


def test_the_connections_open_to_the_machines_are_read_off_the_kernel_by_peer() -> None:
    """Every established connection to a drilled box counts, whichever process and port it is on.

    The forward's tunnels, a `mngr observe`, a shell's own ssh -- all of them
    died in the incident, and all of them share the box's address. Nothing to
    another address, nothing not yet or no longer established.
    """
    connections = parse_established_connections(_NETSTAT_AT_THE_WAKE, {"198.51.100.7", "203.0.113.9"})

    assert connections == [
        EstablishedConnection(local_port=51000, peer_ip="198.51.100.7", peer_port=2222, process="python3.12:502"),
        EstablishedConnection(local_port=51001, peer_ip="203.0.113.9", peer_port=2211, process="python3.12:504"),
        EstablishedConnection(local_port=51002, peer_ip="198.51.100.7", peer_port=22, process="ssh:777"),
    ]


def test_every_app_s_box_is_blocked_by_the_one_anchor() -> None:
    """Two apps behind two boxes have to lose the network on the same packet.

    An anchor covering only the first app would compare an app that saw an
    outage against an app that did not, which is a difference between the runs
    rather than between the builds.
    """
    rules = build_black_hole_rules(["198.51.100.7", "203.0.113.9"], peer_ports=None, local_ports=None)

    assert "block drop out quick proto tcp from any to 198.51.100.7\n" in rules
    assert "block drop in quick proto tcp from 198.51.100.7 to any\n" in rules
    assert "block drop out quick proto tcp from any to 203.0.113.9\n" in rules
    assert "block drop in quick proto tcp from 203.0.113.9 to any\n" in rules


def _stub_pf(tmp_path: Path, *, is_flush_failing: bool) -> Path:
    """A `sudo` that just execs, and a `pfctl` that answers without touching the firewall."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    flush = 'echo "denied" >&2; exit 1' if is_flush_failing else "exit 0"
    (bin_dir / "pfctl").write_text(
        "#!/bin/sh\n"
        'case "$1" in -E) echo "Token : 4242"; exit 0 ;; esac\n'
        f'for a in "$@"; do if [ "$a" = "rules" ]; then {flush}; fi; done\n'
        "exit 0\n"
    )
    for name in ("sudo", "pfctl"):
        (bin_dir / name).chmod(0o755)
    return bin_dir


def _black_hole(tmp_path: Path, bin_dir: Path, deadman_seconds: float, monkeypatch: pytest.MonkeyPatch) -> BlackHole:
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return BlackHole(
        log=EventLog(tmp_path / "events.jsonl", Clocks()),
        peer_ips=["198.51.100.7"],
        peer_ports=None,
        local_ports=None,
        deadman_seconds=deadman_seconds,
    )


@pytest.mark.skipif(not IS_DARWIN, reason="the black hole's helper is written for macOS pf")
def test_a_removal_that_did_not_happen_is_not_reported_as_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure that made a whole run unreadable, and left a laptop cut off.

    A run recorded that it had lifted the block when the pfctl doing it had
    never run, so the log said the network came back and every post-wake
    measurement was taken against one that had not. Answering False is what lets
    the drill say so instead.
    """
    black_hole = _black_hole(tmp_path, _stub_pf(tmp_path, is_flush_failing=True), 120, monkeypatch)
    black_hole.start()
    black_hole.install()

    assert black_hole.remove() is False
    # And it stays False: a retry must not short-circuit on a flag cleared by the
    # attempt that failed.
    assert black_hole.shutdown() is False


@pytest.mark.skipif(not IS_DARWIN, reason="the black hole's helper is written for macOS pf")
def test_the_rule_that_goes_in_names_the_ports_learned_after_the_helper_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connections to kill are known only at the wake, long after root was taken.

    So the rule is written when it goes in, from the ports named by then, and
    cannot be re-scoped underneath a rule that is already loaded.
    """
    black_hole = _black_hole(tmp_path, _stub_pf(tmp_path, is_flush_failing=False), 120, monkeypatch)
    black_hole.start()
    black_hole.narrow_to_local_ports([51002, 51000])
    black_hole.install()

    installed = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
        if json.loads(line)["event"] == "black_hole_installed"
    ]
    assert len(installed) == 1
    assert installed[0]["local_ports"] == [51000, 51002]
    assert "from any port { 51000, 51002 } to 198.51.100.7" in installed[0]["rules"]
    with pytest.raises(BlackHoleError):
        black_hole.narrow_to_local_ports([51003])
    assert black_hole.shutdown() is True


@pytest.mark.skipif(not IS_DARWIN, reason="the black hole's helper is written for macOS pf")
def test_the_rule_comes_out_on_its_own_when_nothing_asks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The deadman, which is what makes the drill safe to kill.

    A run that crashes or is interrupted while holding the network down would
    otherwise leave the laptop unable to reach the workspaces until someone
    noticed and ran pfctl by hand.
    """
    black_hole = _black_hole(tmp_path, _stub_pf(tmp_path, is_flush_failing=False), 1, monkeypatch)
    black_hole.start()
    black_hole.install()
    time.sleep(3.0)

    assert black_hole.remove() is True


def test_a_quiet_but_running_app_is_not_mistaken_for_a_missing_one(tmp_path: Path) -> None:
    """Freshness, not growth: minutes of silence from a healthy app must still pass the pre-flight.

    Measured on a running app, the log went 35s without a byte while everything
    was fine, so anything that waits to watch it grow refuses working setups.
    """
    quiet = tmp_path / "minds-events.jsonl"
    quiet.write_text("{}\n")
    written_at = quiet.stat().st_mtime

    assert log_age_seconds(quiet, written_at + 120.0) == pytest.approx(120.0)
    assert log_age_seconds(tmp_path / "absent.jsonl", written_at) is None


def test_a_dark_wake_does_not_count_as_the_wake_that_ended_the_sleep() -> None:
    """The countdown spends awake seconds only, so a dark wake's few can never reach it.

    On battery macOS dark-wakes partway through a sleep; each one ends the
    interval and fires the app's wake callbacks. Lifting the black hole on one
    would restore the network while the machine is still asleep and time every
    post-wake measurement from the wrong edge.
    """
    settler = WakeSettler(settle_seconds=60.0)
    assert settler.awake_seconds_since_last_gap(1000.0) is None

    settler.on_wake(_wake(1.0))
    gap_at = settler.awake_seconds_since_last_gap(0.0)
    assert gap_at is not None
    # A dark wake: a few awake seconds, then another gap, which restarts the count.
    now = -gap_at + 5.0
    assert not settler.is_settled(now)
    settler.on_wake(_wake(2.0))
    assert not settler.is_settled(now + 30.0)

    # The lid actually opens: 60 uninterrupted awake seconds past the last gap.
    assert settler.is_settled(now + 60.0)


def test_the_apps_own_mngr_is_preferred_over_whatever_is_on_path(tmp_path: Path) -> None:
    """The mngr beside the app's own `minds` is the one that reads the app's host dir.

    PATH may hold a different version, and on a machine running two apps it is
    the wrong one for at least one of them -- so it is only ever the fallback.
    """
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "mngr").write_text("#!/bin/sh\n")
    app = _app(tmp_path, bin_dir)

    assert resolve_app_mngr(app, lambda _: "/usr/local/bin/mngr") == [str(bin_dir / "mngr")]

    (bin_dir / "mngr").unlink()
    assert resolve_app_mngr(app, lambda _: "/usr/local/bin/mngr") == ["/usr/local/bin/mngr"]


def test_a_machine_with_no_mngr_at_all_names_the_app_it_could_not_resolve(tmp_path: Path) -> None:
    """With two apps in the run, "no mngr" has to say which app's mngr is missing."""
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    app = _app(tmp_path, bin_dir)

    with pytest.raises(DrillError, match="no mngr for the app at") as caught:
        resolve_app_mngr(app, lambda _: None)
    assert str(app.data_dir) in str(caught.value)


def test_a_machine_whose_agents_are_all_bare_ids_is_refused_by_name() -> None:
    """The failure a real run hit, from its own log.

    A listing that returned in under four seconds carried agent ids in the name
    field. Nothing on that machine could then be told apart -- including the
    system-services agent the recovery acts on -- so the run is refused, and the
    refusal names discovery rather than the machine, which is what a user has to
    act on.
    """
    services_id = "agent-16c3a7ed2a7b44c2a0c5989d05fe4e31"
    degraded = [
        _agent(services_id, services_id, host_name="workspace-1"),
        _agent(_OTHER_AGENT, _OTHER_AGENT, host_name="workspace-1"),
    ]

    with pytest.raises(DrillError, match="discovery has not named them yet"):
        select_host(degraded, "workspace-1")


def test_a_named_workspace_still_resolves_beside_unnamed_ones() -> None:
    """One machine mid-discovery must not block drilling another that is fully known.

    A real listing had exactly this shape: agents on a reachable host named
    properly, alongside an unreachable host whose agents came back as ids.
    """
    agents = [
        _agent(_WORKSPACE_AGENT, "quiet-amber-heron", display_name="Spare workspace"),
        _agent(_SERVICES_AGENT, "system-services"),
        _agent(_OTHER_AGENT, _OTHER_AGENT, host_id="host-elsewhere", host_name="half-discovered"),
    ]

    assert select_host(agents, "quiet-amber-heron").agent_ids == frozenset({_WORKSPACE_AGENT, _SERVICES_AGENT})


def test_a_workspace_nothing_was_looking_at_is_named_as_the_cause() -> None:
    """The second failed run's shape: the outage was proven, and the app still said nothing.

    A workspace with no window open on it generates no failing requests, so it
    is never enrolled as a probe suspect and the probe loop never looks at it.
    The report has to separate that from an app that saw the outage and ignored
    it, because only one of them is a defect.
    """
    silent = "\n".join(
        build_report(
            app_label="minds",
            target=_target(),
            sightings=[],
            mentions=0,
            wakes=[_wake(1300.0)],
            black_hole_installed_wall=900.0,
            black_hole_removed_wall=1300.0,
        )
    )
    busy = "\n".join(
        build_report(
            app_label="minds",
            target=_target(),
            sightings=[],
            mentions=40,
            wakes=[_wake(1300.0)],
            black_hole_installed_wall=900.0,
            black_hole_removed_wall=1300.0,
        )
    )

    assert "named this workspace 0 times" in silent
    assert "Open a window on this workspace" in silent
    assert "named this workspace 40 times" in busy
    assert "Open a window on this workspace" not in busy


def test_a_host_that_is_not_running_is_refused() -> None:
    """The second failed run targeted a stale duplicate host whose record read UNKNOWN.

    Its address still answered a TCP connect -- something else listens there --
    so reachability alone passed it. There was no live workspace to interrupt,
    which afterwards is indistinguishable from an app ignoring an outage.
    """
    stale = _agent(_WORKSPACE_AGENT, "Chat-1")
    stale["host"]["state"] = "UNKNOWN"
    services = _agent(_SERVICES_AGENT, "system-services")
    services["host"]["state"] = "UNKNOWN"

    with pytest.raises(DrillError, match="no running remote machine"):
        select_host([stale, services], "Chat-1")
