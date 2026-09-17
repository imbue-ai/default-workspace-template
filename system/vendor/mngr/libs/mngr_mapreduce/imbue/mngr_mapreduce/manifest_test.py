import json
from pathlib import Path

from imbue.imbue_common.model_update import to_update
from imbue.mngr_mapreduce.execution import AgentNodeProduct
from imbue.mngr_mapreduce.execution import NodeOutcome
from imbue.mngr_mapreduce.execution import NodeStatus
from imbue.mngr_mapreduce.manifest import MANIFEST_FILENAME
from imbue.mngr_mapreduce.manifest import ManifestWriter
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_mapreduce.testing import make_execution
from imbue.mngr_mapreduce.testing import make_execution_plan


def test_manifest_writer_writes_the_execution_as_readable_json(tmp_path: Path) -> None:
    plan = make_execution_plan()
    plan = plan.model_copy_update(to_update(plan.field_ref().output_dir, tmp_path))
    execution = make_execution(plan).with_node_outcome(
        NodeOutcome(
            node_name=NodeName("map"),
            status=NodeStatus.SUCCEEDED,
            produced=AgentNodeProduct(),
            detail="all good",
        )
    )

    ManifestWriter(output_dir=tmp_path).on_execution_changed(execution)

    written = json.loads((tmp_path / MANIFEST_FILENAME).read_text())
    assert written["pipeline_name"] == "p"
    assert written["node_outcome_by_node_name"]["map"]["status"] == "SUCCEEDED"


def test_manifest_writer_does_not_raise_when_the_output_dir_is_unwritable(tmp_path: Path) -> None:
    """A manifest that cannot be written must never take the run down with it."""
    blocking_file = tmp_path / "blocked"
    blocking_file.write_text("not a directory")

    ManifestWriter(output_dir=blocking_file / "nested").on_execution_changed(make_execution())
