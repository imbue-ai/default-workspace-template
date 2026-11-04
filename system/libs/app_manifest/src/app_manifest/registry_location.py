import os
from pathlib import Path
from typing import Final

# The registry's location, exactly as system/scripts/forward_port.py resolves it:
# relative to the cwd (the repo root under supervisord) unless MINDS_APPS_FILE
# points elsewhere. Kept apart from app_manifest.registry so a caller can find the
# registry without importing pydantic (the workspace-layout command).
DEFAULT_APPS_FILE: Final[str] = "data/.state/apps.toml"
ENV_APPS_FILE: Final[str] = "MINDS_APPS_FILE"


def registry_path() -> Path:
    return Path(os.environ.get(ENV_APPS_FILE, DEFAULT_APPS_FILE))
