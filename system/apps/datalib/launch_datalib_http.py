#!/usr/bin/env python3
"""Register the Datalib app and run datalib-http in this process's place.

datalib-http requires its API token on every route, and a browser gets in by
loading ``/?token=<token>`` once. A window opens at a launch path's ``path``
with its ``presets`` as the query, so the token travels as a preset: this
script writes a copy of ``app.toml`` whose one launch path carries
``presets = { token = "<token>" }``, registers that copy through
``system/scripts/forward_port.py``, and then execs datalib-http with the same
token in ``DATALIB_TOKEN``.

Usage (the ``datalib`` program line, from the repo root):
    python3 system/apps/datalib/launch_datalib_http.py

Standard-library only, like forward_port.py: the program line runs it under a
plain ``python3``, so the app never depends on the root venv being intact. The
one import beyond the standard library is the stdlib-only ``oom_priority``
package, by path, the way ``oom_tag_service.py`` imports it.
"""

import argparse
import fcntl
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Final

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "services" / "oom_priority" / "src")
)

from oom_priority import bands  # noqa: E402

# The app's fixed wiring, relative to the repo root every supervised program runs from.
MANIFEST_PATH: Final[Path] = Path("system/apps/datalib/app.toml")
FORWARD_PORT_PATH: Final[Path] = Path("system/scripts/forward_port.py")
DEFAULT_PORT: Final[int] = 8731
BIND_HOST: Final[str] = "127.0.0.1"

# The skill's folder is laid out like the desktop app's ~/Datalib: a libraries folder holding one
# folder per library, and the store the skill's CLI writes and searches is the default library. The
# UI serves the same one, so a source added through the wizard is what the agent queries and vice versa.
LIBRARIES_DIR: Final[Path] = Path("data/.skills/datalib")
DEFAULT_LIBRARY_NAME: Final[str] = "Default"
DATA_ROOT: Final[Path] = LIBRARIES_DIR / DEFAULT_LIBRARY_NAME

# What marks a directory as holding a store: the config the skill writes, or the directory
# datalib-http keeps its own files (the token, the lock) in.
CONFIG_FILE_NAME: Final[str] = "config.toml"
SYSTEM_DIRECTORY_NAME: Final[str] = "system"

# The files under system/ that a datalib process holds a flock(2) on while it runs: datalib-http
# holds both for its life, and datalib-dag holds ``runner-lock`` while it syncs. The kernel
# releases a lock when its holder exits.
LOCK_FILE_NAMES: Final[tuple[str, ...]] = ("lock", "runner-lock")

# A config's top-level ``data_root`` key, which datalib-dag uses as written in place of the
# config's own folder.
DATA_ROOT_LINE: Final[re.Pattern[str]] = re.compile(
    r'^\s*data_root\s*=\s*"([^"]*)"\s*$'
)

# Where the manifest copy that carries the token is written. Machine state: it is rewritten on
# every start and never read by anything but forward_port.py.
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

# Where the datalib skill's scripts/install_datalib.sh links the pinned binaries. An absolute path:
# supervisord's children do not have ~/.local/bin on PATH.
DATALIB_HTTP_RELATIVE_TO_HOME: Final[Path] = Path(".local") / "bin" / "datalib-http"

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


def _is_data_root(path: Path) -> bool:
    return (path / CONFIG_FILE_NAME).is_file() or (
        path / SYSTEM_DIRECTORY_NAME
    ).is_dir()


def _remove_empty_library_or_refuse_to_move(library: Path, store_to_move: Path) -> None:
    if not library.exists():
        return
    if library.is_dir() and not any(library.iterdir()):
        library.rmdir()
        return
    raise DatalibLaunchError(
        f"{store_to_move} holds a store and the library {library} is not empty; "
        "move one of them aside, then start the app again"
    )


def _require_unlocked(store: Path) -> None:
    for name in LOCK_FILE_NAMES:
        lock_path = store / SYSTEM_DIRECTORY_NAME / name
        if not lock_path.is_file():
            continue
        with lock_path.open("rb") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise DatalibLaunchError(
                    f"a datalib process still holds {lock_path}; the store at {store} is moved on the next start"
                ) from e


def _names_root(line: str, root: Path) -> bool:
    match = DATA_ROOT_LINE.match(line)
    if match is None:
        return False
    return Path(match.group(1)).expanduser().resolve() == root.resolve()


def _drop_data_root_line(config_path: Path, old_root: Path) -> None:
    """Drop a top-level ``data_root`` that names the moved folder, as the desktop app does when it moves a library.

    A config's ``data_root`` defaults to the config's own folder, which moves with it; one written out as the old
    folder would keep the pipeline writing there. Only that line goes: any other value was chosen on purpose.
    """
    if not config_path.is_file():
        return
    kept: list[str] = []
    changed = False
    is_top_level = True
    for line in config_path.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.lstrip().startswith("["):
            is_top_level = False
        if is_top_level and _names_root(line, old_root):
            changed = True
            continue
        kept.append(line)
    if changed:
        config_path.write_text("".join(kept), encoding="utf-8")


# CLEANUP: drop move_store_into_library, its helpers above, and its call in launch() once every
# workspace that used datalib while the store sat at data/.skills/datalib itself (before the
# libraries folder, October 2026) has started the app since.
def move_store_into_library(libraries_dir: Path, data_root: Path) -> bool:
    """Move a store written at the libraries folder itself into ``data_root``, a library inside it; return whether one moved.

    Two renames, each atomic: the folder becomes a staging sibling, and the staging sibling becomes the
    library inside a fresh folder. A start that finds the staging sibling finishes the second rename. A
    data root that is not directly inside the libraries folder (judged by where the two are on disk, not by
    how either path is spelled) is a store somewhere else, left alone.
    """
    if data_root.parent.resolve() != libraries_dir.resolve():
        return False
    staging = libraries_dir.with_name(
        f"{libraries_dir.name}.moving-to-{data_root.name}"
    )
    if not staging.is_dir():
        if not _is_data_root(libraries_dir):
            return False
        _require_unlocked(libraries_dir)
        _remove_empty_library_or_refuse_to_move(data_root, libraries_dir)
        libraries_dir.rename(staging)
    libraries_dir.mkdir(parents=True, exist_ok=True)
    _remove_empty_library_or_refuse_to_move(data_root, staging)
    staging.rename(data_root)
    _drop_data_root_line(data_root / CONFIG_FILE_NAME, libraries_dir)
    logger.info("Moved the store at %s into the library %s", libraries_dir, data_root)
    return True


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
    # forward_port.py reads the icon relative to the manifest it is given.
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


def register_app(
    forward_port_path: Path, registered_manifest_path: Path, port: int
) -> None:
    """Register the manifest copy and datalib-http's port in the app registry."""
    result = subprocess.run(
        [
            sys.executable,
            str(forward_port_path),
            "--manifest",
            str(registered_manifest_path),
            "--url",
            f"http://localhost:{port}",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise DatalibLaunchError(
            f"{forward_port_path} exited {result.returncode}; the app is not registered"
        )


def launch(arguments: argparse.Namespace) -> None:
    """Move an older store into the library, register the app with the token on its launch path, then become datalib-http.

    Refuses to start (so supervisord retries with its backoff, and the app is never registered without a
    server behind it) until the datalib skill's install script has installed the binary, while a datalib
    process still holds the store to move, or while that store and a non-empty library both exist.
    """
    if not arguments.datalib_http.is_file():
        raise DatalibLaunchError(
            f"{arguments.datalib_http} is not installed yet; "
            "run .agents/skills/datalib/scripts/install_datalib.sh, as the datalib skill does on first use"
        )
    move_store_into_library(LIBRARIES_DIR, arguments.data_root)
    token = read_or_mint_token(arguments.data_root)
    registered_manifest_path = write_registered_manifest(
        arguments.manifest, token, arguments.state_directory
    )
    register_app(arguments.forward_port, registered_manifest_path, arguments.port)
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
        help="The datalib data root the UI serves: a library inside the libraries folder",
    )
    parser.add_argument(
        "--datalib-http",
        type=Path,
        default=Path.home() / DATALIB_HTTP_RELATIVE_TO_HOME,
        help="The datalib-http binary to run",
    )
    parser.add_argument(
        "--state-directory",
        type=Path,
        default=STATE_DIRECTORY,
        help="Where the manifest copy that carries the token is written",
    )
    parser.add_argument(
        "--forward-port",
        type=Path,
        default=FORWARD_PORT_PATH,
        help="The registration script",
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
