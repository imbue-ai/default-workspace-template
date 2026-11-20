"""Test helpers: a throwaway workspace tree with user apps in it."""

from pathlib import Path

from app_manifest.testing import write_app_manifest

MINIMAL_PYPROJECT = """\
[project]
name = "{name}"
version = "0.1.0"

[project.scripts]
{name} = "{package}.runner:main"
"""


def manifest_body(name: str, extra: str = "") -> str:
    """A user app's manifest: the scaffold's required fields, then ``extra``."""
    return f'name = "{name}"\ndisplay_name = "{name.title()}"\nicon = "icon.svg"\n{extra}'


def write_user_app(repo_root: Path, name: str, extra_manifest: str = "", is_python: bool = True) -> str:
    """Create ``system/apps/<package>`` for the app ``name`` and return the package directory's name."""
    package = name.replace("-", "_")
    write_app_manifest(repo_root, package, manifest_body(name, extra_manifest), is_icon_written=True)
    if is_python:
        (repo_root / "system" / "apps" / package / "pyproject.toml").write_text(
            MINIMAL_PYPROJECT.format(name=name, package=package)
        )
    return package


def write_app_file(repo_root: Path, package: str, relative_path: str, text: str = "") -> Path:
    """Create a file inside the app's directory."""
    path = repo_root / "system" / "apps" / package / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def write_workspace_file(repo_root: Path, relative_path: str, text: str = "") -> Path:
    """Create a file elsewhere in the workspace tree."""
    path = repo_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def never_bound(port: int) -> bool:
    """A port check for a tree nothing listens in."""
    return False
