"""Writing the execution to disk after every change, so a run can be read while it happens."""

from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.mngr_mapreduce.execution import Execution
from imbue.mngr_mapreduce.interfaces import ExecutionObserverInterface

MANIFEST_FILENAME: Final[str] = "execution.json"


class ManifestWriter(ExecutionObserverInterface):
    """Writes the whole execution to ``<output_dir>/execution.json`` after every change.

    Nothing reads it back to resume a run; resumability is deliberately out of
    scope for this version. It exists so an operator, a report, or another
    process can see where a long run has got to.
    """

    output_dir: Path = Field(frozen=True, description="The execution's output directory")

    def on_execution_changed(self, execution: Execution) -> None:
        manifest_path = self.output_dir / MANIFEST_FILENAME
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(execution.model_dump_json(indent=2, exclude_computed_fields=True))
        except OSError as exc:
            logger.warning("Failed to write the execution manifest at {}: {}", manifest_path, exc)
