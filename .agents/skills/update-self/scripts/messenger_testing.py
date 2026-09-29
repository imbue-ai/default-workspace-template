"""Recording stand-ins for the messengers ``run_in_background.py`` delivers through."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any


class RecordingMessengers:
    """A recording ``mngr`` on PATH, and a recording ``message_chat.py`` to put in a tree.

    Each call appends one JSON line to ``record``: which messenger ran, its argv, and the
    text of the ``--message-file`` it was given.
    """

    def __init__(self, bin_dir: Path, record: Path) -> None:
        self.record = record
        self._write_recorder(bin_dir / "mngr", "mngr")

    def install_message_chat(self, tree: Path) -> None:
        self._write_recorder(
            tree / "system" / "scripts" / "message_chat.py", "message_chat.py"
        )

    def calls(self) -> list[dict[str, Any]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def _write_recorder(self, path: Path, name: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "argv = sys.argv[1:]\n"
            "text = open(argv[argv.index('--message-file') + 1]).read() if '--message-file' in argv else None\n"
            f"with open({str(self.record)!r}, 'a') as handle:\n"
            f"    handle.write(json.dumps({{'messenger': {name!r}, 'argv': argv, 'text': text}}) + '\\n')\n"
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
