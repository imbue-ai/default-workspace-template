import json
import shlex
import subprocess
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.minds_evals import diagnostic_probe
from imbue.minds_evals.data_types import DiagnosticProbeReading
from imbue.minds_evals.data_types import ProbeAgent
from imbue.minds_evals.data_types import ProbeTicket
from imbue.minds_evals.testing import TICKET_FILE_TEXT_BY_NAME
from imbue.minds_evals.testing import behaviour_feed_fixture
from imbue.minds_evals.testing import lay_out_behaviour_workspace

# A worker's own step ticket beside the lead's, read out of a live behaviour trial's workspace snapshot: the
# workers share the lead's ticket directory.
_WORKER_TICKET_TEXT: Final[str] = (
    "---\nclosed: 2026-09-14T08:39:48.310198Z\nstarted: 2026-09-14T08:39:42.961417Z\nid: dw76-step-daln\n"
    "status: closed\ndeps: []\nlinks: []\ncreated: 2026-09-14T08:39:40.759928Z\ntype: task\npriority: 2\n"
    "agent: diag-worker-7f3a\nstep: true\n---\n# Write the finish report\n\n\n## Summary\n\nWrote the short completion note.\n"
)

# `mngr list --format json` as a live claude behaviour trial's workspace printed it, less the fields the
# probe does not keep.
_LISTING: Final[dict[str, Any]] = {
    "agents": [
        {
            "id": "agent-0670b90384eb40bcb3be11b0344507e2",
            "name": "system-services",
            "type": "main",
            "command": "sleep infinity",
            "state": "WAITING",
            "labels": {"project": "behaviour", "user_created": "true", "is_primary": "true"},
        },
        {
            "id": "agent-3cceb2702bab4fd2a2a9d4d658e5926b",
            "name": "EVAL-behaviour-bwdkzkg-67c40b82",
            "type": "claude",
            "state": "WAITING",
            "labels": {"project": "behaviour", "first": "true", "user_created": "true"},
        },
        {
            "id": "agent-35d4e01c80284d9eaff230ee8416cf38",
            "name": "diag-worker-7f3a",
            "type": "claude",
            "state": "WAITING",
            "labels": {"project": "workspace", "agent_created": "true"},
        },
    ],
    "errors": [],
}


def _lay_out_workspace(tmp_path: Path) -> Path:
    """A repo laid out as a behaviour trial leaves one at its second step's collection."""
    repo = tmp_path / "workspace"
    lay_out_behaviour_workspace(
        repo,
        {**TICKET_FILE_TEXT_BY_NAME, "dw76-step-daln.md": _WORKER_TICKET_TEXT},
        is_report_written=True,
        is_upload_placed=True,
    )
    return repo


def _run_probe(repo: Path, agent_listing_command: str) -> str:
    completed = subprocess.run(
        ["sh", "-c", diagnostic_probe.diagnostic_probe_command(str(repo), agent_listing_command)],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _listing_command(tmp_path: Path, listing_text: str) -> str:
    listing_path = tmp_path / "agents.json"
    listing_path.write_text(listing_text)
    return "cat {}".format(shlex.quote(str(listing_path)))


def test_the_probe_prints_the_workspaces_tickets_agents_reports_and_uploads_and_its_parser_reads_them(
    tmp_path: Path,
) -> None:
    """The command runs for real against a laid-out repo, so what the parser is pinned against is what the
    probe prints."""
    repo = _lay_out_workspace(tmp_path)

    output = _run_probe(repo, _listing_command(tmp_path, json.dumps(_LISTING)))

    assert diagnostic_probe.parse_diagnostic_probe(output) == DiagnosticProbeReading(
        tickets=(
            ProbeTicket(title="Write the finish report", status="closed", is_step=True),
            ProbeTicket(title="DIAG regular ticket 7f3a", status="open", is_step=False),
            ProbeTicket(title="DIAG beta 7f3a", status="closed", is_step=True),
            ProbeTicket(title="DIAG alpha 7f3a", status="closed", is_step=True),
        ),
        agents=(
            ProbeAgent(
                name="system-services",
                agent_type="main",
                labels={"project": "behaviour", "user_created": "true", "is_primary": "true"},
            ),
            ProbeAgent(
                name="EVAL-behaviour-bwdkzkg-67c40b82",
                agent_type="claude",
                labels={"project": "behaviour", "first": "true", "user_created": "true"},
            ),
            ProbeAgent(
                name="diag-worker-7f3a", agent_type="claude", labels={"project": "workspace", "agent_created": "true"}
            ),
        ),
        report_paths=(diagnostic_probe.WORKER_REPORT_PATH,),
        upload_paths=("data/uploads/README.md", diagnostic_probe.UPLOAD_MARKER_PATH),
    )


def test_a_workspace_with_nothing_in_it_yet_reads_as_empty_rather_than_unknown(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()

    output = _run_probe(repo, _listing_command(tmp_path, json.dumps({"agents": []})))

    assert diagnostic_probe.parse_diagnostic_probe(output) == DiagnosticProbeReading(
        tickets=(), agents=(), report_paths=(), upload_paths=()
    )


def test_a_listing_that_is_not_json_reads_as_unknown_agents_and_keeps_everything_else(tmp_path: Path) -> None:
    repo = _lay_out_workspace(tmp_path)

    output = _run_probe(repo, "echo 'mngr: could not reach the host' >&2; exit 1")

    reading = diagnostic_probe.parse_diagnostic_probe(output)
    assert reading is not None
    assert reading.agents is None
    assert reading.report_paths == (diagnostic_probe.WORKER_REPORT_PATH,)
    assert len(reading.tickets) == 4


@pytest.mark.parametrize(
    "output",
    [
        pytest.param("", id="nothing"),
        pytest.param("mngr exec: timed out\n", id="no-section"),
        pytest.param(
            "==== minds_evals_probe:tickets\n==== minds_evals_probe:ticket data/.tickets/a.md\n---\nstatus: closed\n",
            id="cut-short",
        ),
    ],
)
def test_a_probe_that_did_not_run_to_its_end_reads_as_nothing(output: str) -> None:
    assert diagnostic_probe.parse_diagnostic_probe(output) is None


def test_a_probe_whose_repo_is_missing_reads_as_nothing(tmp_path: Path) -> None:
    completed = subprocess.run(
        ["sh", "-c", diagnostic_probe.diagnostic_probe_command(str(tmp_path / "no-repo"), "true")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert diagnostic_probe.parse_diagnostic_probe(completed.stdout) is None


@pytest.mark.parametrize(
    ("harness", "expected_step_ids", "expected_tool_call_count"),
    [
        pytest.param("claude", ["wor-step-5xxu", "wor-step-qa99"], 15, id="claude"),
        pytest.param("pi-coding", ["wor-step-70ly", "wor-step-x1hi"], 20, id="pi-coding"),
        pytest.param("codex", ["wor-step-vbja", "wor-step-yz0b"], 15, id="codex"),
    ],
)
def test_the_feed_readings_find_the_commanded_step_ids_and_every_tool_call_on_each_harness(
    harness: str, expected_step_ids: list[str], expected_tool_call_count: int
) -> None:
    """Read off each harness's feed as a live behaviour cell's driver polled it. pi-coding's agent
    created step tickets of its own at the second step, which are not the commanded ones, so the
    reading has to match on the nonce titles rather than on a count."""
    events = behaviour_feed_fixture(harness)

    stamps = diagnostic_probe.feed_tk_stamps(events)
    assert diagnostic_probe.step_ids_by_title(stamps, diagnostic_probe.STEP_TICKET_TITLES) == expected_step_ids
    tool_calls = diagnostic_probe.feed_tool_calls(events)
    assert len(tool_calls) == expected_tool_call_count
    assert all(event_id and call_id for event_id, call_id in tool_calls)


def test_the_feeds_tool_inputs_are_merged_across_detail_payloads_and_a_missing_payload_adds_none() -> None:
    detail_by_event_id = {
        "evt-1-assistant": {"inputs_by_tool_call_id": {"call-1": '{"command": "echo DIAG-7f3a-one"}'}, "output": None},
        "evt-2-assistant": {"inputs_by_tool_call_id": {"call-2": "diag-missing-command-7f3a"}},
        "evt-3-assistant": None,
        "evt-4-tool_result": {"inputs_by_tool_call_id": {}, "output": "DIAG-7f3a-one"},
    }

    assert diagnostic_probe.feed_tool_input_by_call_id(detail_by_event_id) == {
        "call-1": '{"command": "echo DIAG-7f3a-one"}',
        "call-2": "diag-missing-command-7f3a",
    }
