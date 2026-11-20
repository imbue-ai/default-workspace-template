"""Picking a free port for an installed app, by the build-app scaffolder's rules.

A port is taken when any supervisord program config (the main file or a drop-in) names it in a localhost URL, when
the app registry holds a row at it, when another install of the store holds it, when it is on the reserved list, or
when something is listening on it right now. The lowest port at or above 8080 that is none of those is picked.
"""

import re
import socket
import tomllib
from collections.abc import Callable
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from loguru import logger

from app_store.errors import PortError
from imbue.imbue_common.pure import pure

LOWEST_AUTO_PORT: Final[int] = 8080
HIGHEST_AUTO_PORT: Final[int] = 8999
# Held by services that do not show in a supervisord URL: the browser's CDP proxy.
RESERVED_PORTS: Final[frozenset[int]] = frozenset({8083})
LOCALHOST_PORT_PATTERN: Final[re.Pattern[str]] = re.compile(r"https?://(?:localhost|127\.0\.0\.1):(\d+)")


@pure
def ports_named_in(text: str) -> set[int]:
    """Every port a localhost URL in ``text`` names."""
    return {int(match.group(1)) for match in LOCALHOST_PORT_PATTERN.finditer(text)}


def ports_in_supervisord_configs(supervisord_conf: Path) -> set[int]:
    """The ports the main supervisord config and every drop-in beside it name."""
    paths = [supervisord_conf, *sorted((supervisord_conf.parent / "supervisord.conf.d").glob("*.conf"))]
    ports: set[int] = set()
    for path in paths:
        if path.is_file():
            ports |= ports_named_in(path.read_text(encoding="utf-8"))
    return ports


def ports_in_registry(apps_toml: Path) -> set[int]:
    """The ports the app registry's rows are at; none when it is missing or unreadable."""
    if not apps_toml.is_file():
        return set()
    try:
        document = tomllib.loads(apps_toml.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read the app registry {} for its ports: {}", apps_toml, e)
        return set()
    rows = document.get("apps", [])
    return {port for row in rows if isinstance(row, dict) for port in ports_named_in(str(row.get("url", "")))}


def is_port_bound(port: int) -> bool:
    """Whether something is listening on (or holds) ``127.0.0.1:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def pick_free_port(
    supervisord_conf: Path,
    apps_toml: Path,
    held_by_store: Iterable[int],
    is_bound: Callable[[int], bool] = is_port_bound,
) -> int:
    """The lowest free port at or above ``LOWEST_AUTO_PORT``; raises PortError when the range is exhausted."""
    taken = (
        ports_in_supervisord_configs(supervisord_conf)
        | ports_in_registry(apps_toml)
        | set(held_by_store)
        | RESERVED_PORTS
    )
    for port in range(LOWEST_AUTO_PORT, HIGHEST_AUTO_PORT + 1):
        if port not in taken and not is_bound(port):
            return port
    raise PortError(f"no free port between {LOWEST_AUTO_PORT} and {HIGHEST_AUTO_PORT}")
