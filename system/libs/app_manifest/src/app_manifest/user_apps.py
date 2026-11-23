"""Whether the workspace holds an app its user built, as opposed to one the template came with."""

from collections.abc import Iterator
from pathlib import Path
from pathlib import PurePosixPath
from typing import Final

from loguru import logger

from app_manifest.errors import ManifestLoadError
from app_manifest.manifest import APPS_DIRECTORY_PARTS
from app_manifest.manifest import MANIFEST_FILENAME
from app_manifest.manifest import load_manifest
from app_manifest.primitives import AppName
from app_manifest.registry import read_registry
from app_manifest.workspace_graph import read_python_members
from app_manifest.workspace_graph import read_user_packages

_APPS_DIRECTORY: Final[str] = "/".join(APPS_DIRECTORY_PARTS)


def _user_built_app_names(repo_root: Path) -> Iterator[AppName]:
    """The name each user package under the apps directory registers under: its manifest's. A package with no
    manifest is not an app, and one whose manifest does not load (logged) cannot have registered."""
    for directory in read_user_packages(repo_root, read_python_members(repo_root)):
        if PurePosixPath(directory).parent.as_posix() != _APPS_DIRECTORY:
            continue
        manifest_path = repo_root / directory / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        try:
            manifest = load_manifest(manifest_path, repo_root=repo_root)
        except ManifestLoadError as e:
            logger.warning("Not counting {} as a built app, because its manifest does not load: {}", directory, e)
            continue
        yield manifest.name


def has_user_built_app(repo_root: Path, registry: Path) -> bool:
    """Whether the registry at ``registry`` holds an app the user of the workspace at ``repo_root`` built.

    Such an app is a row that is neither ``system`` nor ``internal`` and that a user package registered: a package
    under the apps directory that the root ``pyproject.toml`` does not list as a workspace member, under its
    manifest's name. Every app the template came with is a member, or has no package at all, so none counts,
    whatever flags its row carries. Neither does anything shown in a frame: a frame's own row (a new app's mock, or
    a preview of a change) belongs to no package, and the row of the app behind one is ``internal`` for as long as
    the frame is up. So an app counts from the moment its user can open it as itself.

    Raises AppManifestError when the registry or the root ``pyproject.toml`` cannot be read.
    """
    openable_names = {row.name for row in read_registry(registry) if not row.system and not row.internal}
    if not openable_names:
        return False
    return any(name in openable_names for name in _user_built_app_names(repo_root))
