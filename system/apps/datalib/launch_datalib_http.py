#!/usr/bin/env python3
"""Register the Datalib app and run datalib-http in this process's place.

datalib-http requires its API token on every route, and a browser gets in by
loading ``/?token=<token>`` once. A window opens at a launch path's ``path``
with its ``presets`` as the query, so the token travels as a preset: this
script writes a copy of ``app.toml`` whose one launch path carries
``presets = { token = "<token>" }``, registers that copy through the
``app_registration`` library, and then execs datalib-http with the same
token in ``DATALIB_TOKEN``.

Usage (the ``datalib`` program line, from the repo root):
    python3 system/apps/datalib/launch_datalib_http.py

The program line runs it under a plain ``python3``: its only imports beyond the
standard library are the stdlib-only ``oom_priority`` and ``app_registration``
packages, which it puts on ``sys.path`` the way ``oom_tag_service.py`` imports
``oom_priority``.
"""

import argparse
import logging
import os
import re
import secrets
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Final

# import-path: no-venv -- its only imports beyond the standard library are the stdlib-only `oom_priority` and `app_registration`
sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "services" / "oom_priority" / "src")
)
# import-path: no-venv -- its only imports beyond the standard library are the stdlib-only `oom_priority` and `app_registration`
sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "libs" / "app_registration" / "src")
)

from app_registration import forward_port  # noqa: E402
from oom_priority import bands  # noqa: E402

# The app's fixed wiring, relative to the repo root every supervised program runs from.
MANIFEST_PATH: Final[Path] = Path("system/apps/datalib/app.toml")
DEFAULT_PORT: Final[int] = 8731
BIND_HOST: Final[str] = "127.0.0.1"

# The workspace's shared data, as opposed to one app's own data under data/.apps/<app>/: datalib
# manages it, for the datalib skill and this app alike. The folder is the store's data root, the one
# the skill's CLI writes and searches and the UI serves, so a source added through the wizard is what
# the agent queries and vice versa.
DATA_ROOT: Final[Path] = Path("data/.shared-data")

# Where the manifest copy that carries the token is written. Machine state: it is rewritten on
# every start and never read by anything but the registration.
STATE_DIRECTORY: Final[Path] = Path("data/.state/datalib-app")

# datalib-http reads both from its environment: where to listen, and the token to require
# (auth.rs, TOKEN_ENV) instead of minting one per process.
ENV_BIND: Final[str] = "DATALIB_BIND"
ENV_TOKEN: Final[str] = "DATALIB_TOKEN"

# Who may frame datalib's app page (embed.rs, FRAME_ANCESTORS_ENV): every place Studio shows it
# from. One fixed list, so nothing changes when the workspace is shared or moves domain.
ENV_FRAME_ANCESTORS: Final[str] = "DATALIB_FRAME_ANCESTORS"

# Desktop: the Studio window's own page is http://localhost on a random port, and the app is a
# nested frame on https://<label>.agent-<hex>.localhost:<forward port>. Only pages served from
# the laptop itself match these.
_DESKTOP_FRAME_ANCESTORS: Final[tuple[str, ...]] = (
    "http://localhost:*",
    "http://127.0.0.1:*",
    "https://*.localhost:*",
)

# Share: the workspace shell on the share domain, under one of Imbue's content domains, inside
# the hosted chrome. Every tier's are listed (mngr's apps/minds config envs). These also admit
# other users' shares; the share gateway's Caddy appends frame-ancestors for this workspace's
# share domain alone, and the browser enforces both.
_SHARE_CONTENT_DOMAINS: Final[tuple[str, ...]] = (
    "personal-imbue.com",
    "personal-imbue-staging.com",
    "personal-imbue-dev.com",
)
_CHROME_ORIGINS: Final[tuple[str, ...]] = (
    "https://studio.imbue.com",
    "https://minds.imbue.com",
    "https://studio.imbue-staging.com",
    "https://minds.imbue-staging.com",
)

FRAME_ANCESTORS: Final[str] = " ".join(
    (
        *_DESKTOP_FRAME_ANCESTORS,
        *(f"https://*.{domain}" for domain in _SHARE_CONTENT_DOMAINS),
        *_CHROME_ORIGINS,
    )
)

# datalib-http's query key for the token on a launch URL (auth.rs, TOKEN_QUERY_KEY).
TOKEN_QUERY_KEY: Final[str] = "token"

# datalib-http's own rule for a token handed in through DATALIB_TOKEN (auth.rs, is_url_safe):
# the unreserved URL characters, so it can ride a query string unencoded.
TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._~-]+$")

# The shape datalib-http mints itself: 64 hex characters.
MINTED_TOKEN_BYTES: Final[int] = 32

# Where datalib-http publishes the token it runs with, relative to the data root; the datalib
# skill reads the same file for its bearer token.
TOKEN_FILE_RELATIVE_PATH: Final[Path] = Path("system") / "api-token"

# Where the datalib skill's scripts/install_datalib.sh links the pinned binaries: under the home
# that holds the workspace, which the install script also derives from its own location rather than
# from $HOME (an agent's shell has HOME=/root). An absolute path: supervised programs do not have
# that bin directory on PATH.
WORKSPACE_HOME: Final[Path] = Path(__file__).resolve().parents[3].parent
DATALIB_HTTP_PATH: Final[Path] = WORKSPACE_HOME / ".local" / "bin" / "datalib-http"

EXIT_NOT_READY: Final[int] = 1

# datalib is not announced to users yet, so under memory pressure it goes before every agent and
# every service: the band of an agent's own subprocesses. A sync's steps inherit it from
# datalib-http. The program line's `user` band is the floor the backstop holds it to, and the
# backstop only ever raises a process, so it leaves this higher value alone.
OOM_SCORE_ADJ: Final[int] = bands.AGENT_SUBPROCESS

# Diagnostics go to stderr, which supervisord captures as the program's log.
logger: Final[logging.Logger] = logging.getLogger("launch_datalib_http")


class DatalibLaunchError(Exception):
    """The Datalib app could not be registered or started; the message says why."""


def token_file_path(data_root: Path) -> Path:
    """Where datalib-http writes the token for a store at ``data_root``."""
    return data_root / TOKEN_FILE_RELATIVE_PATH


def mint_token() -> str:
    """A fresh random token, in the shape datalib-http mints for itself."""
    return secrets.token_hex(MINTED_TOKEN_BYTES)


def read_or_mint_token(data_root: Path) -> str:
    """The token the last datalib-http on ``data_root`` ran with, or a fresh one when there is none usable.

    Reusing the published token keeps it stable across restarts of the program, so a browser that
    already holds datalib's session cookie stays signed in and the skill's copy stays valid. datalib-http
    rewrites the file with whatever it is started with, so a token this cannot reuse is simply replaced.
    """
    path = token_file_path(data_root)
    if not path.is_file():
        return mint_token()
    published = path.read_text(encoding="utf-8", errors="replace").strip()
    if not TOKEN_PATTERN.fullmatch(published):
        logger.warning(
            "The token file %s does not hold a usable token; minting a new one", path
        )
        return mint_token()
    return published


def manifest_text_with_token(manifest_text: str, token: str) -> str:
    """``manifest_text`` with the token as a preset of its last table, which must be its one launch path.

    The standard library reads TOML but does not write it, so the preset is appended as one more key
    of the manifest's final ``[[launch_paths]]`` table, and the result is parsed to confirm the key
    landed there.
    """
    if not TOKEN_PATTERN.fullmatch(token):
        raise DatalibLaunchError(
            "invalid datalib API token: expected one or more of A-Z a-z 0-9 - . _ ~"
        )
    with_token = (
        f'{manifest_text.rstrip()}\npresets = {{ {TOKEN_QUERY_KEY} = "{token}" }}\n'
    )
    try:
        launch_paths = tomllib.loads(with_token).get("launch_paths", [])
    except tomllib.TOMLDecodeError as e:
        raise DatalibLaunchError(
            f"the manifest is not valid TOML once the token preset is appended: {e}"
        ) from e
    if len(launch_paths) != 1 or launch_paths[0].get("presets") != {
        TOKEN_QUERY_KEY: token
    }:
        raise DatalibLaunchError(
            "the manifest must end with its one [[launch_paths]] table, so the token preset can be appended to it"
        )
    return with_token


def write_registered_manifest(
    manifest_path: Path, token: str, state_directory: Path
) -> Path:
    """Write the manifest copy that carries the token, with its icon beside it, and return its path."""
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
        icon_name = tomllib.loads(manifest_text)["icon"]
    except (OSError, tomllib.TOMLDecodeError, KeyError) as e:
        raise DatalibLaunchError(
            f"cannot read the manifest {manifest_path} and its icon name: {e}"
        ) from e
    state_directory.mkdir(parents=True, exist_ok=True)
    # The registration reads the icon relative to the manifest it is given.
    shutil.copyfile(manifest_path.parent / icon_name, state_directory / icon_name)
    registered_path = state_directory / manifest_path.name
    registered_path.write_text(
        manifest_text_with_token(manifest_text, token), encoding="utf-8"
    )
    return registered_path


def build_datalib_http_argv(datalib_http_path: Path, data_root: Path) -> list[str]:
    """The datalib-http command line: the data root, and no browser to open (the window is the browser)."""
    return [str(datalib_http_path), "--no-open", str(data_root)]


def child_environment(port: int, token: str) -> dict[str, str]:
    """What datalib-http reads from its environment: the loopback bind, the token to require, and who may frame it."""
    return {
        ENV_BIND: f"{BIND_HOST}:{port}",
        ENV_TOKEN: token,
        ENV_FRAME_ANCESTORS: FRAME_ANCESTORS,
    }


def register_app(registered_manifest_path: Path, port: int) -> None:
    """Register the manifest copy and datalib-http's port in the app registry."""
    try:
        forward_port.register_manifest(
            forward_port.apps_file(),
            registered_manifest_path,
            f"http://localhost:{port}",
        )
    except (forward_port.RegistryError, OSError, ValueError) as e:
        raise DatalibLaunchError(f"the app is not registered: {e}") from e


def launch(arguments: argparse.Namespace) -> None:
    """Register the app with the token on its launch path, then become datalib-http.

    Refuses to start (so supervisord retries with its backoff, and the app is never registered without a
    server behind it) until the datalib skill's install script has installed the binary.
    """
    if not arguments.datalib_http.is_file():
        raise DatalibLaunchError(
            f"{arguments.datalib_http} is not installed yet; "
            "run .agents/skills/datalib/scripts/install_datalib.sh, as the datalib skill does on first use"
        )
    token = read_or_mint_token(arguments.data_root)
    registered_manifest_path = write_registered_manifest(
        arguments.manifest, token, arguments.state_directory
    )
    register_app(registered_manifest_path, arguments.port)
    if not bands.set_oom_score_adj(os.getpid(), OOM_SCORE_ADJ):
        logger.warning(
            "Could not set oom_score_adj to %d; datalib-http keeps the band it was started in",
            OOM_SCORE_ADJ,
        )
    # The token goes through the environment, never the command line, which any process listing shows.
    environment = {**os.environ, **child_environment(arguments.port, token)}
    argv = build_datalib_http_argv(arguments.datalib_http, arguments.data_root)
    os.execve(argv[0], argv, environment)


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register the Datalib app and run datalib-http"
    )
    parser.add_argument(
        "--manifest", type=Path, default=MANIFEST_PATH, help="The app.toml to register"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="The loopback port datalib-http serves on",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DATA_ROOT,
        help="The datalib data root the UI serves",
    )
    parser.add_argument(
        "--datalib-http",
        type=Path,
        default=DATALIB_HTTP_PATH,
        help="The datalib-http binary to run",
    )
    parser.add_argument(
        "--state-directory",
        type=Path,
        default=STATE_DIRECTORY,
        help="Where the manifest copy that carries the token is written",
    )
    return parser.parse_args(argv)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(name)s: %(levelname)s: %(message)s"
    )
    try:
        launch(parse_arguments(sys.argv[1:]))
    except DatalibLaunchError as e:
        logger.error("%s", e)
        sys.exit(EXIT_NOT_READY)


if __name__ == "__main__":
    main()
