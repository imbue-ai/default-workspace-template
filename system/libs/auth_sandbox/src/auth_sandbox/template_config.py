"""The template ``config.json`` the image carries: minds' part of latchkey's config, plus the browser entry.

``imbue.mngr_latchkey.core.merge_minds_latchkey_config`` writes the hidden built-in services and the registered
additional services, which is what every gateway that serves minds agents carries. The browser entry is
latchkey's own shape (its ``BrowserConfigSchema``): the executable, how it was found, and when. ``source`` is
``system`` because the launcher is a system-installed browser, not one Playwright found or downloaded.

The sign-in commands merge minds' part into the sandbox's copy again before they run latchkey, as the desktop's
``Latchkey.initialize`` does, but without its ``latchkey --version`` check: the image build pins and checks the
version, and the check costs a node start on every command.
"""

import json
from datetime import datetime
from pathlib import Path

from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.core import (
    CONFIG_FILENAME,
    LatchkeyError,
    merge_minds_latchkey_config,
)

from auth_sandbox.runtime_files import write_private_file

BROWSER_CONFIG_KEY = "browser"
_SYSTEM_BROWSER_SOURCE = "system"


@pure
def render_template_config(browser_executable: Path, discovered_at: datetime) -> str:
    """The config text with minds' blocks and a browser entry naming ``browser_executable``."""
    config = json.loads(merge_minds_latchkey_config(None))
    config[BROWSER_CONFIG_KEY] = {
        "executablePath": str(browser_executable),
        "source": _SYSTEM_BROWSER_SOURCE,
        "discoveredAt": discovered_at.isoformat().replace("+00:00", "Z"),
    }
    return json.dumps(config, indent=2) + "\n"


def merge_minds_config(latchkey_directory: Path) -> None:
    """Write minds' part of ``config.json`` into the directory's config, keeping everything else (the browser
    entry included), as ``Latchkey.initialize`` does.

    Raises:
        LatchkeyError: when the config there is not a JSON object, naming its path.
    """
    config_path = latchkey_directory / CONFIG_FILENAME
    existing = (
        config_path.read_text(encoding="utf-8") if config_path.is_file() else None
    )
    try:
        content = merge_minds_latchkey_config(existing)
    except LatchkeyError as e:
        raise LatchkeyError(f"the latchkey config at {config_path}: {e}") from e
    write_private_file(config_path, content.encode("utf-8"))
