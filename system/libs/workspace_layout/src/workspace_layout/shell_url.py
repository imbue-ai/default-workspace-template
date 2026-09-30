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


def shell_base_url() -> str:
    return os.environ.get(ENV_SHELL_URL, DEFAULT_SHELL_URL).rstrip("/")
