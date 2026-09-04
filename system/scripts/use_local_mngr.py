#!/usr/bin/env python3
"""Make this workspace run the mngr tree at ``system/vendor/mngr``, if one is present.

``pyproject.toml`` pins mngr and its plugins to one commit of the public mngr repo
under ``[tool.uv.sources]``. Dropping an mngr checkout at ``system/vendor/mngr``
(untracked; ``just minds-start`` and the CI harnesses in the mngr repo do this)
overrides that: ``build_workspace.sh`` runs this first, and it rewrites every one of
those entries to an editable path into the tree and relocks, e.g.

    imbue-mngr = { git = "https://github.com/imbue-ai/mngr", rev = "<commit>", subdirectory = "libs/mngr" }

becomes

    imbue-mngr = { path = "system/vendor/mngr/libs/mngr", editable = true }

Everything downstream (``list_mngr_plugins.py``, ``uv sync``, the update-self refresh)
reads the rewritten file, so the tool environments and the workspace venv all resolve
from the tree. Without the tree this is a no-op, and re-running on a rewritten file is
a no-op too. ``git checkout -- pyproject.toml uv.lock`` puts the pin back.
"""

import re
import subprocess
import sys
from pathlib import Path

LOCAL_MNGR_DIR = Path("system/vendor/mngr")
PUBLIC_MNGR_REPO = "https://github.com/imbue-ai/mngr"
_PINNED_SOURCE = re.compile(
    rf'^(?P<name>[A-Za-z0-9_.-]+) = \{{ git = "{re.escape(PUBLIC_MNGR_REPO)}", rev = "[0-9a-f]+", '
    r'subdirectory = "(?P<subdirectory>[^"]+)" \}$',
    re.MULTILINE,
)


def has_local_mngr(repo_root: Path) -> bool:
    return (repo_root / LOCAL_MNGR_DIR / "libs" / "mngr" / "pyproject.toml").is_file()


def rewrite_sources(pyproject_text: str) -> str:
    """Point every entry pinned to the public mngr repo at the same subdirectory of the local tree."""
    return _PINNED_SOURCE.sub(
        lambda match: f'{match["name"]} = {{ path = "{LOCAL_MNGR_DIR.as_posix()}/{match["subdirectory"]}", editable = true }}',
        pyproject_text,
    )


def use_local_mngr(repo_root: Path) -> bool:
    """Rewrite and relock if a local tree is present; returns whether anything changed."""
    if not has_local_mngr(repo_root):
        return False
    pyproject = repo_root / "pyproject.toml"
    before = pyproject.read_text()
    after = rewrite_sources(before)
    if after == before:
        return False
    pyproject.write_text(after)
    subprocess.run(["uv", "lock"], cwd=repo_root, check=True)
    return True


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(argv[0] if argv else ".").resolve()
    if use_local_mngr(repo_root):
        print(f"pointed pyproject.toml at the local mngr tree in {LOCAL_MNGR_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
