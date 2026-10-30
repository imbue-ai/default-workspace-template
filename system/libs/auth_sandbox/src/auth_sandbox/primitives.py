"""Names shared by the sandbox's commands, its boot script, the image build and the connector that mints it.

The paths and variable names here are a contract with two other parties: the Imbue Studio web chrome, which
runs the commands over owner-exec and writes the files they read, and the connector, which creates the sandbox
with the ``AUTH_SANDBOX_*`` environment (the web latchkey auth sandbox spec, sections 5 and 6).
"""

from pathlib import Path
from typing import Final

# Everything user-specific the flow writes lives under this tmpfs, which dies with the sandbox and is never
# snapshotted: the sandbox's own latchkey key, the dialog's transfer key, the incoming browser state and the
# export the chrome reads back.
RUNTIME_DIR: Final[Path] = Path("/run/auth-sandbox")
LATCHKEY_KEY_FILENAME: Final[str] = "latchkey_key"
TRANSFER_KEY_FILENAME: Final[str] = "transfer_key"
INCOMING_DIRNAME: Final[str] = "incoming"
EXPORT_DIRNAME: Final[str] = "export"
LATCHKEY_DIRNAME: Final[str] = "latchkey"

# What the image build installs, outside the workspace tree so the sandbox's command is a fixed path.
INSTALL_DIR: Final[Path] = Path("/opt/auth-sandbox")
BOOT_SCRIPT_PATH: Final[Path] = INSTALL_DIR / "boot.sh"
SUPERVISORD_CONF_PATH: Final[Path] = INSTALL_DIR / "supervisord.conf"
# The template latchkey ``config.json``: minds' hidden built-ins and registered services, plus the browser entry
# naming the Fortress launcher. The boot script copies it into the sandbox's latchkey directory.
TEMPLATE_CONFIG_PATH: Final[Path] = INSTALL_DIR / "config.json"
WORKSPACE_ROOT: Final[Path] = Path("/home/user/workspace")

# The Fortress launcher the Screen's env.d unit installs (system/scripts/env.d/2000-screen.sh); latchkey launches
# it through Playwright as its configured browser (D10).
FORTRESS_LAUNCHER_PATH: Final[Path] = Path("/opt/fortress/tilion-fortress/tilion")

# The Screen's display and audio, as system/apps/screen names them (``screen.primitives`` and
# ``screen.neko_config``); repeated here because the Screen is an app with its own tool environment, not a
# library this one can import.
SCREEN_DISPLAY: Final[str] = ":99"
SCREEN_RUNTIME_DIR: Final[str] = "/tmp/screen-runtime"
SCREEN_PULSE_SERVER: Final[str] = f"unix:{SCREEN_RUNTIME_DIR}/pulse.sock"
SCREEN_SERVICE_URL: Final[str] = "http://127.0.0.1:8090"

# The non-secret environment the connector passes at sandbox create (spec 5.2) that the boot script and
# the commands read from the process environment (it also passes AUTH_SANDBOX_ID and
# AUTH_SANDBOX_CONNECTOR_URL, which nothing here reads).
HOST_ID_ENV_VAR: Final[str] = "AUTH_SANDBOX_HOST_ID"
REGION_ENV_VAR: Final[str] = "AUTH_SANDBOX_REGION"
WORKSPACE_HOST_ID_ENV_VAR: Final[str] = "AUTH_SANDBOX_WORKSPACE_HOST_ID"
# Optional overrides of what the region implies for the launcher's geo pins.
TIMEZONE_ENV_VAR: Final[str] = "AUTH_SANDBOX_TIMEZONE"
COUNTRY_ENV_VAR: Final[str] = "AUTH_SANDBOX_COUNTRY"
# Which ``latchkey`` the commands run; the npm global on PATH unless set.
LATCHKEY_BINARY_ENV_VAR: Final[str] = "AUTH_SANDBOX_LATCHKEY_BINARY"
DEFAULT_LATCHKEY_BINARY: Final[str] = "latchkey"

# The Fortress ``tilion`` launcher's environment (spec section 15, spike result 3): no activation prompt,
# the geo pinned so no network lookup sits on the sign-in's critical path, and one fingerprint seed per
# workspace so a service sees one device across that workspace's sign-ins.
TILION_NO_ACTIVATE_ENV_VAR: Final[str] = "TILION_NO_ACTIVATE"
TILION_TZ_ENV_VAR: Final[str] = "TILION_TZ"
TILION_COUNTRY_ENV_VAR: Final[str] = "TILION_COUNTRY"
TILION_SEED_ENV_VAR: Final[str] = "TILION_SEED"

LATCHKEY_DIRECTORY_ENV_VAR: Final[str] = "LATCHKEY_DIRECTORY"
LATCHKEY_GATEWAY_ENV_VAR: Final[str] = "LATCHKEY_GATEWAY"
LATCHKEY_DISABLE_COUNTING_ENV_VAR: Final[str] = "LATCHKEY_DISABLE_COUNTING"
DISPLAY_ENV_VAR: Final[str] = "DISPLAY"
XDG_RUNTIME_DIR_ENV_VAR: Final[str] = "XDG_RUNTIME_DIR"
PULSE_SERVER_ENV_VAR: Final[str] = "PULSE_SERVER"
