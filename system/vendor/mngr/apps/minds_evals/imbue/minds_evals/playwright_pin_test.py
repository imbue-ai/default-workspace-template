"""The box's playwright and this project's playwright are one pin.

The UI-flow step script runs in the box against the monorepo venv, whose playwright is whatever the
root lock resolves for apps/minds. The flow lab runs the same script here, against this project's
own playwright, and a flow's page state is playwright's aria snapshot of a playwright-installed
Chromium: a different release renders a different tree and drives a different browser. The lab
only reproduces what the box sees while the two locks agree.
"""

import tomllib
from pathlib import Path

_PROJECT_DIR = Path(__file__).parent.parent.parent
_ROOT_DIR = _PROJECT_DIR.parent.parent


def _locked_version(lock_path: Path, package: str) -> str:
    packages = [entry for entry in tomllib.loads(lock_path.read_text())["package"] if entry["name"] == package]
    assert len(packages) == 1, "expected exactly one {} in {}, found {}".format(package, lock_path, len(packages))
    return packages[0]["version"]


def test_playwright_pin_matches_the_box() -> None:
    """A lab run on a different playwright than the box's measures a different instrument."""
    declared = [
        entry
        for entry in tomllib.loads((_PROJECT_DIR / "pyproject.toml").read_text())["project"]["dependencies"]
        if str(entry).startswith("playwright")
    ]
    assert declared == ["playwright=={}".format(_locked_version(_ROOT_DIR / "uv.lock", "playwright"))], (
        "apps/minds_evals pins {} but the root lock (which the box installs) holds playwright {}; pin "
        "them equal and re-lock, or the flow lab drives a different browser than the box".format(
            declared, _locked_version(_ROOT_DIR / "uv.lock", "playwright")
        )
    )
    assert _locked_version(_PROJECT_DIR / "uv.lock", "playwright") == _locked_version(
        _ROOT_DIR / "uv.lock", "playwright"
    )
