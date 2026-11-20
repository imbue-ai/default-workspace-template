from pathlib import Path
from typing import Final

# Every path is relative to the workspace repo's root, the directory the command is run from.
APPS_DIRECTORY: Final[Path] = Path("system/apps")
SUPERVISORD_CONF: Final[Path] = Path("system/supervisord.conf")
SUPERVISORD_DROPIN_DIRECTORY: Final[Path] = Path("system/supervisord.conf.d")
SKILLS_DIRECTORY: Final[Path] = Path(".agents/skills")
ROUTINES_DIRECTORY: Final[Path] = Path("system/routines")
ENVIRONMENT_UNITS_DIRECTORY: Final[Path] = Path("system/scripts/env.d")
INSTALL_LOCK_PATH: Final[Path] = Path("data/.state/app_install/lock")
APP_DATA_DIRECTORY: Final[Path] = Path("data/.apps")
SECRETS_DIRECTORY: Final[Path] = Path("data/.secrets")
# The local ref that keeps the commit an installed app was last taken from alive in the workspace repository.
UPSTREAM_REF_PREFIX: Final[str] = "refs/app-upstream/"
MANIFEST_FILENAME: Final[str] = "app.toml"
PYPROJECT_FILENAME: Final[str] = "pyproject.toml"
APP_SKILLS_DIRNAME: Final[str] = "skills"
APP_ROUTINES_DIRNAME: Final[str] = "routines"
APP_ENVIRONMENT_UNITS_DIRNAME: Final[str] = "env.d"
SKILL_FILENAME: Final[str] = "SKILL.md"


def app_directory(repo_root: Path, package: str) -> Path:
    return repo_root / APPS_DIRECTORY / package


def manifest_path(repo_root: Path, package: str) -> Path:
    return app_directory(repo_root, package) / MANIFEST_FILENAME


def program_conf_path(repo_root: Path, program: str) -> Path:
    return repo_root / SUPERVISORD_DROPIN_DIRECTORY / f"{program}.conf"


def skill_link_path(repo_root: Path, skill: str) -> Path:
    return repo_root / SKILLS_DIRECTORY / skill
