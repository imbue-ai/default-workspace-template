"""The scripts beside this file, importable by the tests beside it.

They are flat scripts, not a package, so each is loaded from its file under a
name of its own. The tests import them from here rather than from
``conftest``: a rootless ``conftest.py`` is module ``conftest`` for the whole
pytest process, and a run that collects several directories at once (offload
batches do) may have imported another directory's by the time a test here
runs its own imports.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


def load_script_module(module_name: str, filename: str) -> Any:
    """Import one of the scripts beside this file under ``module_name``."""
    spec = importlib.util.spec_from_file_location(
        module_name, Path(__file__).parent / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


layout = load_script_module("layout_for_fixtures", "layout.py")
message_chat = load_script_module("message_chat_for_fixtures", "message_chat.py")
run_in_background = load_script_module(
    "run_in_background_for_fixtures", "run_in_background.py"
)
seed_welcome_chat = load_script_module(
    "seed_welcome_chat_for_fixtures", "seed_welcome_chat.py"
)
