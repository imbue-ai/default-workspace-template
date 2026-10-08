import os
from typing import Final

# Where the shell listens: MINDS_WORKSPACE_SERVER_URL, else its loopback port.
DEFAULT_SHELL_URL: Final[str] = "http://127.0.0.1:8000"
ENV_SHELL_URL: Final[str] = "MINDS_WORKSPACE_SERVER_URL"

# The shell's routes this library reaches (desktop contracts.md sections 5 and 8).
LAYOUT_OP_ROUTE: Final[str] = "/api/layout/broadcast"
INVENTORY_ROUTE: Final[str] = "/api/inventory"
CLIENTS_ROUTE: Final[str] = "/api/clients"
DESKTOPS_ROUTE: Final[str] = "/api/desktops"
CLIENT_ACTIVITY_ROUTE: Final[str] = "/api/client-activity"

# The status the op route refuses an op with when it would change where a window the target client has popped out
# into its own window sits, and the op did not carry ``force`` (plan-popped-out-layout-ops.md).
POPPED_OUT_REFUSAL_STATUS: Final[int] = 423


def shell_base_url() -> str:
    return os.environ.get(ENV_SHELL_URL, DEFAULT_SHELL_URL).rstrip("/")
