#!/usr/bin/env python3
"""Entry point for :mod:`publish_template_skill.write_template_manifest`."""

import sys
from pathlib import Path

# Run without the workspace venv (`uv run --no-project`, and from a snapshot of this
# skill outside the repo), so the package beside scripts/ is put on sys.path here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from publish_template_skill.write_template_manifest import main

if __name__ == "__main__":
    sys.exit(main())
