from pathlib import Path
from typing import Final

# Every path is relative to the workspace repo's root, the directory the command is run from.
APPS_DIRECTORY: Final[Path] = Path("system/apps")
SUPERVISORD_CONF: Final[Path] = Path("system/supervisord.conf")
SUPERVISORD_DROPIN_DIRECTORY: Final[Path] = Path("system/supervisord.conf.d")
SKILLS_DIRECTORY: Final[Path] = Path(".agents/skills")
ROUTINES_DIRECTORY: Final[Path] = Path("system/routines")
ENVIRONMENT_UNITS_DIRECTORY: Final[Path] = Path("system/scripts/env.d")
# What the installer keeps about this machine: its lock, an update that waits for a person, and data snapshots.
STATE_DIRECTORY: Final[Path] = Path("data/.state/app_install")
INSTALL_LOCK_PATH: Final[Path] = STATE_DIRECTORY / "lock"
APP_DATA_DIRECTORY: Final[Path] = Path("data/.apps")
SECRETS_DIRECTORY: Final[Path] = Path("data/.secrets")
# Who may visit each app of a shared workspace, in one table per app keyed by the app's name.
SHARE_GRANTS_PATH: Final[Path] = SECRETS_DIRECTORY / "share_grants.toml"
# What the routines library keeps about each routine on this machine, in one directory per routine id.
ROUTINE_STATE_DIRECTORY: Final[Path] = Path("data/.state/routines")
# The local ref that keeps the commit an installed app was last taken from alive in the workspace repository.
UPSTREAM_REF_PREFIX: Final[str] = "refs/app-upstream/"
# The local ref that keeps the commit last published from this workspace alive: the next publish's parent.
PUBLISHED_REF_PREFIX: Final[str] = "refs/app-published/"
MANIFEST_FILENAME: Final[str] = "app.toml"
PYPROJECT_FILENAME: Final[str] = "pyproject.toml"
APP_SKILLS_DIRNAME: Final[str] = "skills"
APP_ROUTINES_DIRNAME: Final[str] = "routines"
APP_ENVIRONMENT_UNITS_DIRNAME: Final[str] = "env.d"
SKILL_FILENAME: Final[str] = "SKILL.md"


def supervisord_conf_files(repo_root: Path) -> list[Path]:
    """The supervisord config and every drop-in beside it, in the order supervisord reads them.

    The drop-in directory is fixed by convention rather than read out of the config's ``[include]`` glob, which
    ``system/test_supervisord_layout.py`` holds every reader to. Only regular files: supervisord cannot read a
    directory named like a drop-in either.
    """
    files = [repo_root / SUPERVISORD_CONF]
    files.extend(sorted((repo_root / SUPERVISORD_DROPIN_DIRECTORY).glob("*.conf")))
    return [path for path in files if path.is_file()]


def app_directory(repo_root: Path, package: str) -> Path:
    return repo_root / APPS_DIRECTORY / package


def manifest_path(repo_root: Path, package: str) -> Path:
    return app_directory(repo_root, package) / MANIFEST_FILENAME


def program_conf_path(repo_root: Path, program: str) -> Path:
    return repo_root / SUPERVISORD_DROPIN_DIRECTORY / f"{program}.conf"


def skill_link_path(repo_root: Path, skill: str) -> Path:
    return repo_root / SKILLS_DIRECTORY / skill


def secret_file_path(secret_file: str) -> Path:
    """Where the secret file a manifest declares as ``secret_file`` is kept, relative to the repo root."""
    return SECRETS_DIRECTORY / f"{secret_file}.env"
