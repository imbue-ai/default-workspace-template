"""Shared fixtures for the publish scripts' tests: git, and a finished template tree."""

import subprocess
from pathlib import Path

FINISHED_TEMPLATE_TOML = """format = "v2"

[template]
slug = "demo"
title = "Demo"
description = "A demo."
thumbnail = "template.svg"
version = "v1"

[recipe]
include = ["system/apps/demo"]
"""

FINISHED_TEMPLATE_MARKDOWN = """---
title: Demo
description: A demo.
thumbnail: template.svg
format: v2
---

# Demo
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def init_repo(root: Path) -> Path:
    """An empty repo on `main` at `root`, with a commit identity set."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main", ".")
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "T")
    return root


def write_finished_template(root: Path, readme: str) -> None:
    """The manifest pair, a real thumbnail and a README: a tree the validator passes."""
    (root / "template.toml").write_text(FINISHED_TEMPLATE_TOML)
    (root / "template.md").write_text(FINISHED_TEMPLATE_MARKDOWN)
    (root / "template.svg").write_text("<svg></svg>")
    (root / "README.md").write_text(readme)
