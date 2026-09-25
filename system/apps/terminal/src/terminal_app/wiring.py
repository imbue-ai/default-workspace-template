"""The fixed wiring both terminal programs share, relative to the repo root every supervised program runs from."""

from pathlib import Path
from typing import Final

STATE_DIR: Final[Path] = Path("data/.state/terminal")
TTYD_EXECUTABLE: Final[str] = "ttyd"
# The tagging wrapper every terminal session runs its shell through (the terminal-session band).
OOM_TAG_SCRIPT: Final[Path] = Path("system/services/oom_priority/bin/oom_tag_service.py")
