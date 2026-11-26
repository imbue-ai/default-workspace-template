"""``apply-initial-data``: the boot sequence's step that applies the document a workspace was handed at create.

Run from the workspace's root each time the workspace's services are started, after the workspace's Python
packages are in place and before the clock is set, so a timezone the document carries is in force from that very
start (``bootstrap.manager``). It exits 0 whatever it found: the boot goes on, and the status the shell answers
says how the initial data stands.
"""

import os
import time
from pathlib import Path
from typing import Final

from loguru import logger

from workspace_initial_data.apply import apply_initial_data

# Where mngr keeps the machine's own state, which is where whoever creates the workspace leaves the document.
HOST_DIR_ENV_VAR: Final[str] = "MNGR_HOST_DIR"


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
        record_backup_destination=None,
        now=time.time(),
    )
    logger.info("The workspace's initial data: {}", status.state.value)
