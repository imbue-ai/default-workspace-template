"""Names shared by the sandbox's commands, its boot script, the image build and the connector that mints it.

The paths and variable names here are a contract with two other parties: the Imbue Studio web chrome, which
runs the commands over owner-exec and writes the session file they read, and the connector, which creates the
sandbox with the ``AUTH_SANDBOX_*`` environment and claims it for a user with the claim file (the web latchkey
auth sandbox spec, and the warm pool's contract).
"""

from pathlib import Path
from typing import Final

# Everything user-specific the flow writes lives under this tmpfs, which dies with the sandbox and is never
# snapshotted: the session's latchkey key, its fingerprint settings, the Screen's owner token digest and the
# latchkey directory the chrome reads the credentials back from.
RUNTIME_DIR: Final[Path] = Path("/run/auth-sandbox")
LATCHKEY_KEY_FILENAME: Final[str] = "latchkey_key"
SESSION_FILENAME: Final[str] = "session.json"
SESSION_SETTINGS_FILENAME: Final[str] = "session_settings.json"
SCREEN_TOKEN_SHA256_FILENAME: Final[str] = "screen_token_sha256"
LATCHKEY_DIRNAME: Final[str] = "latchkey"

# The connector writes the claim when it hands the sandbox to a user, which for a sandbox made on demand may be
# before the boot script has created RUNTIME_DIR, so it lives outside it, as the connect-info file does.
CLAIM_PATH: Final[Path] = Path("/run/auth-sandbox-claim.json")
# The whole of owner-exec's authorized_keys: empty from boot until the claim names the user's key.
AUTHORIZED_KEYS_PATH: Final[Path] = Path("/home/user/.ssh/authorized_keys")

# What the image build installs, outside the workspace tree so the sandbox's command is a fixed path.
INSTALL_DIR: Final[Path] = Path("/opt/auth-sandbox")
BOOT_SCRIPT_PATH: Final[Path] = INSTALL_DIR / "boot.sh"
SUPERVISORD_CONF_PATH: Final[Path] = INSTALL_DIR / "supervisord.conf"
# The template latchkey ``config.json``: minds' hidden built-ins and registered services, plus the browser entry
# naming the sign-in browser script. The boot script copies it into the sandbox's latchkey directory.
TEMPLATE_CONFIG_PATH: Final[Path] = INSTALL_DIR / "config.json"
# The browser latchkey launches for a sign-in: the Fortress launcher with its window at the display's corner.
SIGN_IN_BROWSER_PATH: Final[Path] = INSTALL_DIR / "sign-in-browser.sh"
# This library's own environment, synced from its own lock: no workspace venv carries it.
VENV_DIRNAME: Final[str] = "venv"
WORKSPACE_ROOT: Final[Path] = Path("/home/user/workspace")

# The Fortress launcher the Screen's env.d unit installs (system/scripts/env.d/2000-screen.sh); latchkey launches
# it through Playwright, by way of the sign-in browser script, as its configured browser (D10).
FORTRESS_LAUNCHER_PATH: Final[Path] = Path("/opt/fortress/tilion-fortress/tilion")

# The Screen's display and audio, as system/apps/screen names them (``screen.primitives`` and
# ``screen.neko_config``); repeated here because the Screen is an app with its own tool environment, not a
# library this one can import.
SCREEN_DISPLAY: Final[str] = ":99"
SCREEN_RUNTIME_DIR: Final[str] = "/tmp/screen-runtime"
SCREEN_PULSE_SERVER: Final[str] = f"unix:{SCREEN_RUNTIME_DIR}/pulse.sock"
SCREEN_SERVICE_URL: Final[str] = "http://127.0.0.1:8090"
# The connect-info document the connector writes once it has read the sandbox's tunnels (the raw TCP one to
# neko's ICE mux) and the Screen reads on every connect-info request; outside RUNTIME_DIR, which is root-only.
CONNECT_INFO_PATH: Final[Path] = Path("/run/auth-sandbox-connect-info.json")

# The non-secret environment the connector passes at sandbox create that the boot script and the commands read
# from the process environment: owner-exec's audience is ``container:<host id>``, the sign-in pins the browser's
# country (and, without a timezone from the session, its timezone) from the region, and the hosted chrome's
# origin is owner-exec's CORS origin and the Screen's frame-ancestors.
HOST_ID_ENV_VAR: Final[str] = "AUTH_SANDBOX_HOST_ID"
REGION_ENV_VAR: Final[str] = "AUTH_SANDBOX_REGION"
CHROME_ORIGIN_ENV_VAR: Final[str] = "AUTH_SANDBOX_CHROME_ORIGIN"
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
