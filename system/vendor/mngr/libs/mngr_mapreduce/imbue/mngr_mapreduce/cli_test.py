"""Unit tests for framework CLI helpers."""

from pathlib import Path
from typing import Any

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.utils.testing import make_test_agent_details
from imbue.mngr_mapreduce.cli import RUN_NAME_LABEL_KEY
from imbue.mngr_mapreduce.cli import disable_modal_initial_snapshot
from imbue.mngr_mapreduce.cli import emit_agents_launched
from imbue.mngr_mapreduce.cli import emit_report_path
from imbue.mngr_mapreduce.cli import emit_task_count
from imbue.mngr_mapreduce.cli import resolve_reintegrate_task_id
from imbue.mngr_mapreduce.cli import select_run_mappers
from imbue.mngr_mapreduce.data_types import AgentKind
from imbue.mngr_mapreduce.launching import ROLE_LABEL_KEY
from imbue.mngr_mapreduce.launching import TASK_ID_LABEL_KEY


def _human_output_opts() -> OutputOptions:
    return OutputOptions(output_format=OutputFormat.HUMAN)


def test_emit_task_count_human(capsys: object) -> None:
    emit_task_count(5, _human_output_opts())


def test_emit_agents_launched_human(capsys: object) -> None:
    emit_agents_launched(3, _human_output_opts())


def test_emit_report_path_human(capsys: object, tmp_path: object) -> None:
    emit_report_path(Path("/tmp/report.html"), _human_output_opts())


def test_emit_task_count_json() -> None:
    emit_task_count(10, OutputOptions(output_format=OutputFormat.JSON))


def test_emit_agents_launched_jsonl(capsys: Any) -> None:
    emit_agents_launched(7, OutputOptions(output_format=OutputFormat.JSONL))
    captured = capsys.readouterr()
    assert '"event": "agents_launched"' in captured.out


def test_emit_report_path_json() -> None:
    emit_report_path(Path("/tmp/report.html"), OutputOptions(output_format=OutputFormat.JSON))


def test_emit_report_path_jsonl() -> None:
    emit_report_path(Path("/tmp/report.html"), OutputOptions(output_format=OutputFormat.JSONL))


def test_emit_task_count_jsonl(capsys: Any) -> None:
    emit_task_count(3, OutputOptions(output_format=OutputFormat.JSONL))
    captured = capsys.readouterr()
    assert '"event": "tasks_discovered"' in captured.out


def test_disable_modal_initial_snapshot_skips_non_modal_providers(temp_mngr_ctx: MngrContext) -> None:
    """A non-modal provider name leaves config.providers untouched."""
    before = dict(temp_mngr_ctx.config.providers)
    disable_modal_initial_snapshot(temp_mngr_ctx, "local")
    disable_modal_initial_snapshot(temp_mngr_ctx, "docker")
    assert dict(temp_mngr_ctx.config.providers) == before


def test_disable_modal_initial_snapshot_silent_when_modal_backend_unregistered(
    temp_mngr_ctx: MngrContext,
) -> None:
    """With --provider modal but no modal backend registered (the test fixture
    only registers local + ssh), the helper silently no-ops; the caller will
    surface the UnknownBackendError later when it tries to actually use modal.
    """
    before = dict(temp_mngr_ctx.config.providers)
    disable_modal_initial_snapshot(temp_mngr_ctx, "modal")
    assert dict(temp_mngr_ctx.config.providers) == before


# --- reintegrate agent discovery ---

_RUN_NAME = "20260101000000"
_OTHER_RUN_NAME = "20260202000000"
_PYTEST_NODE_ID_TASK_ID = "libs/mngr/imbue/mngr/api/create_test.py::test_create_agent[case/one]"


def _make_run_agent(
    name: str,
    kind: AgentKind,
    run_name: str = _RUN_NAME,
    task_id: str | None = None,
) -> AgentDetails:
    """Build the AgentDetails `mngr list` would report for one agent of a run."""
    labels = {RUN_NAME_LABEL_KEY: run_name, ROLE_LABEL_KEY: kind.value}
    if task_id is not None:
        labels[TASK_ID_LABEL_KEY] = task_id
    return make_test_agent_details(name=name, labels=labels)


def test_select_run_mappers_skips_the_reducer_and_the_snapshotter() -> None:
    """Every agent of a run carries its run-name label, but only mappers publish mapper outputs.

    Selecting on the run name alone drags the snapshotter into the report as
    a mapper whose outputs could not be pulled.
    """
    mapper = _make_run_agent("tmr-mapper", AgentKind.MAPPER)
    reducer = _make_run_agent("tmr-reducer", AgentKind.REDUCER)
    snapshotter = _make_run_agent("tmr-snapshotter", AgentKind.SNAPSHOTTER)
    other_run_mapper = _make_run_agent("tmr-other-mapper", AgentKind.MAPPER, run_name=_OTHER_RUN_NAME)

    selected = select_run_mappers([reducer, snapshotter, mapper, other_run_mapper], _RUN_NAME)

    assert [detail.name for detail in selected] == [mapper.name]


def test_select_run_mappers_returns_nothing_when_no_agent_matches_the_run() -> None:
    snapshotter = _make_run_agent("tmr-snapshotter", AgentKind.SNAPSHOTTER)

    assert select_run_mappers([snapshotter], _OTHER_RUN_NAME) == []


def test_reintegrate_task_id_comes_from_the_task_id_label() -> None:
    detail = _make_run_agent("tmr-mapper", AgentKind.MAPPER, task_id=_PYTEST_NODE_ID_TASK_ID)

    assert resolve_reintegrate_task_id(detail) == _PYTEST_NODE_ID_TASK_ID


def test_reintegrate_task_id_falls_back_to_the_agent_name_for_an_unlabelled_run() -> None:
    """Runs launched before the task-id label existed still reintegrate."""
    detail = _make_run_agent("tmr-mapper", AgentKind.MAPPER)

    assert resolve_reintegrate_task_id(detail) == "tmr-mapper"
