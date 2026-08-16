"""Migrate claude state left under a legacy /root home into the current home tree.

Workspaces created before the /home/user home layout (pre-minds-v0.3.16 lima
and modal creates) ran every process with HOME=/root, so claude's state --
session transcripts, sign-in credentials, settings, plugins, and the global
``.claude.json`` -- accumulated under ``/root/.claude`` (plus
``/root/.claude.json`` beside it). Updating such a workspace repoints root's
home at the persistent /home/user tree (the same passwd edit fresh creates
apply at provision time), after which every claude resolves ``~/.claude`` to
``/home/user/.claude`` -- a freshly created, empty directory. Nothing is
deleted, but the workspace looks wiped: no chat history, and a sign-in prompt.

The migration runs on every bootstrap (see ``manager.main``), which covers
both moments that matter: the service restart that applies an update-self
pass, and any later boot of a workspace an earlier update already stranded.
It fires only when the resolved home is not /root, the legacy config dir
holds real user state (chat transcripts or credentials), and the current
home's config dir holds no chat transcripts of its own -- so a lived-in new
home is never clobbered, and a fresh workspace (whose /root/.claude never
existed or holds no user state) is untouched.

# CLEANUP: remove this module (and its call in manager.main) once every
# workspace created before the /home/user home layout has booted at least once
# on a release that contains it -- after that, /root/.claude can never hold
# real, unmigrated user state.
"""

import os
import shutil
from pathlib import Path
from typing import Final

from loguru import logger

# The home directory pre-/home/user-layout workspaces ran everything from.
LEGACY_ROOT_HOME: Final[Path] = Path("/root")

# Where the destination's pre-existing global config file is preserved when the
# legacy one replaces it (it only ever records the post-update first start).
REPLACED_CLAUDE_JSON_NAME: Final[str] = ".claude.json.pre-migration"


def _has_session_transcripts(claude_config_dir: Path) -> bool:
    """Whether the config dir holds at least one real chat session transcript."""
    projects_dir = claude_config_dir / "projects"
    if not projects_dir.is_dir():
        return False
    return next(projects_dir.glob("*/*.jsonl"), None) is not None


def _has_migratable_user_state(claude_config_dir: Path) -> bool:
    """Whether the config dir holds state worth migrating: transcripts or a sign-in."""
    if _has_session_transcripts(claude_config_dir):
        return True
    return (claude_config_dir / ".credentials.json").is_file()


def _align_ownership_with_home(moved_path: Path, home_uid: int, home_gid: int) -> None:
    """Best-effort chown of a migrated tree to the home directory's owner.

    In every current deployment both trees are root-owned and this is a no-op;
    it exists so a migrated entry can never end up unreadable to the home's
    owner if the two ever differ. Stops at the first failure (the remaining
    entries would fail the same way).
    """
    paths = [moved_path]
    if moved_path.is_dir() and not moved_path.is_symlink():
        for dirpath, dirnames, filenames in os.walk(moved_path):
            paths.extend(Path(dirpath) / name for name in dirnames + filenames)
    for candidate in paths:
        try:
            stat = candidate.lstat()
            if stat.st_uid != home_uid or stat.st_gid != home_gid:
                os.lchown(candidate, home_uid, home_gid)
        except OSError as e:
            logger.warning("Failed to align ownership of migrated {}: {}", candidate, e)
            return


def migrate_legacy_claude_state(legacy_home: Path, current_home: Path) -> bool:
    """Move claude state from a legacy home into the current home tree.

    Returns True when anything was migrated. Idempotent: after a successful
    migration the legacy config dir no longer holds user state, so re-runs
    no-op. Skipped entirely when ``CLAUDE_CONFIG_DIR`` is set (claude state is
    then not home-resolved, so the home switch cannot have stranded it).
    """
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return False
    if legacy_home == current_home:
        return False
    legacy_config_dir = legacy_home / ".claude"
    current_config_dir = current_home / ".claude"
    if not _has_migratable_user_state(legacy_config_dir):
        return False
    if _has_session_transcripts(current_config_dir):
        logger.warning(
            "Found legacy claude state at {} but {} already holds chat transcripts; leaving both in place",
            legacy_config_dir,
            current_config_dir,
        )
        return False

    logger.info("Migrating legacy claude state from {} into {}", legacy_home, current_home)
    current_config_dir.mkdir(parents=True, exist_ok=True)
    home_stat = current_home.stat()
    moved_paths: list[Path] = []

    # Move every top-level config entry that does not collide with one already
    # at the destination. In the stranded layout the destination holds only
    # what the post-update services freshly created (e.g. an empty backups/
    # dir), so in practice everything real moves; a colliding entry is left in
    # the legacy tree rather than overwriting anything at the destination.
    for entry in sorted(legacy_config_dir.iterdir()):
        destination = current_config_dir / entry.name
        if destination.exists() or destination.is_symlink():
            logger.warning(
                "Leaving legacy claude entry {} in place: {} already exists",
                entry,
                destination,
            )
            continue
        shutil.move(str(entry), str(destination))
        moved_paths.append(destination)

    # The global config file lives BESIDE the config dir, not inside it. The
    # guard above established that the destination layout has never carried a
    # chat, so a destination copy only records the post-update first start;
    # the legacy one (onboarding state, key approvals) replaces it, with the
    # fresh file preserved alongside for inspection.
    legacy_claude_json = legacy_home / ".claude.json"
    if legacy_claude_json.is_file():
        current_claude_json = current_home / ".claude.json"
        if current_claude_json.exists():
            current_claude_json.replace(current_home / REPLACED_CLAUDE_JSON_NAME)
        shutil.move(str(legacy_claude_json), str(current_claude_json))
        moved_paths.append(current_claude_json)

    for moved in moved_paths:
        _align_ownership_with_home(moved, home_stat.st_uid, home_stat.st_gid)
    logger.info("Migrated {} legacy claude entries into {}", len(moved_paths), current_home)
    return True
