"""Attributing the model calls of a routine's runs in the workspace's usage."""

from typing import Final

from workspace_usage.context import ORIGIN_ID
from workspace_usage.context import ORIGIN_KIND
from workspace_usage.context import ORIGIN_KIND_ROUTINE
from workspace_usage.context import RUN_ID
from workspace_usage.context import format_usage_context

from routines.primitives import RoutineId
from routines.primitives import RunId

HOST_DIR_ENV_VAR: Final[str] = "MNGR_HOST_DIR"


def routine_usage_context(routine_id: RoutineId, run_id: RunId | None) -> str:
    """The IMBUE_USAGE_CONTEXT of a routine's run, or of the routine alone when ``run_id`` is None."""
    context = {ORIGIN_KIND: ORIGIN_KIND_ROUTINE, ORIGIN_ID: str(routine_id)}
    if run_id is not None:
        context[RUN_ID] = str(run_id)
    return format_usage_context(context)
