"""The template ``config.json`` the image carries: minds' part of latchkey's config, plus the browser entry.

``imbue.mngr_latchkey.core.merge_minds_latchkey_config`` writes the hidden built-in services and the registered
additional services, which is what every gateway that serves minds agents carries. The browser entry is
latchkey's own shape (its ``BrowserConfigSchema``): the executable, how it was found, and when. ``source`` is
``system`` because the launcher is a system-installed browser, not one Playwright found or downloaded.
"""

import json
from datetime import datetime
from pathlib import Path

from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.core import merge_minds_latchkey_config

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
