"""``apply-initial-data``: the boot sequence's step that applies the document a workspace was handed at create.

Run from the workspace's root each time the workspace's services are started, after the workspace's Python
packages are in place and before the clock is set, so a timezone the document carries is in force from that very
start (``bootstrap.manager``). It exits 0 whatever it found: the boot goes on, and the status the shell answers
says how the initial data stands.
"""

import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger
from workspace_backup_setup.destination import request_backup_destination
from workspace_backup_setup.state import (
    backup_lease_path,
    workspace_id_from_environment,
)

from workspace_initial_data.apply import apply_initial_data

# Where mngr keeps the machine's own state, which is where whoever creates the workspace leaves the document.
HOST_DIR_ENV_VAR: Final[str] = "MNGR_HOST_DIR"


def record_backup_destination(
    workspace_root: Path, request: Mapping[str, object]
) -> object:
    """Hand the document's backup part to the workspace's own backup setup, as the destination's route does.

    Nothing is set up here: the request is recorded, and ``backup-setup`` acts on it once the services run.
    """
    return request_backup_destination(
        workspace_root,
        request,
        lease_path=backup_lease_path(),
        now=time.time(),
        workspace_id=workspace_id_from_environment(os.environ),
    )


def main() -> None:
    host_dir = os.environ.get(HOST_DIR_ENV_VAR, "")
    if not host_dir:
        logger.info(
            "{} is not set, so there is no host directory to find initial data in",
            HOST_DIR_ENV_VAR,
        )
        return
    status = apply_initial_data(
        Path(host_dir),
        Path.cwd(),
        record_backup_destination=record_backup_destination,
        now=time.time(),
    )
    logger.info("The workspace's initial data: {}", status.state.value)
