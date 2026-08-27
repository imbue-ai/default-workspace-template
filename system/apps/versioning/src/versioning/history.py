import tomllib
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure

from versioning.data_types import MILESTONE_KINDS
from versioning.data_types import AppHistory
from versioning.data_types import AppNotFoundError
from versioning.data_types import AppRef
from versioning.data_types import CommitRecord
from versioning.data_types import VersionNode
from versioning.interfaces import GitRepoInterface

APPS_SUBDIRECTORY: Final[str] = "system/apps"

# The workspace UI itself appears in the list as "System": its history is as much
# the user's as any app's. It is browse-only (see restore.py) because reviving it
# safely needs the update-system-interface machinery, not a plain restart.
SYSTEM_APP_DIR: Final[str] = "system_interface"
SYSTEM_APP_TITLE: Final[str] = "System"

# This app is "versioning" to supervisord and to the registry, but nobody outside
# the code calls it that: to a reader it is where an app's history lives, and the
# tab it opens in is titled History. Only the display name changes.
SELF_APP_DIR: Final[str] = "versioning"
SELF_APP_TITLE: Final[str] = "History"

_TITLE_BY_PACKAGE_DIR: Final[dict[str, str]] = {
    SYSTEM_APP_DIR: SYSTEM_APP_TITLE,
    SELF_APP_DIR: SELF_APP_TITLE,
}


@pure
def _humanize_app_name(service_name: str) -> str:
    for package_dir, title in _TITLE_BY_PACKAGE_DIR.items():
        if service_name == _service_name_for_package_dir(package_dir):
            return title
    return service_name.replace("-", " ").replace("_", " ").title()


@pure
def _service_name_for_package_dir(package_dir_name: str) -> str:
    return package_dir_name.replace("_", "-")


def _read_registry_entries_by_app_name(apps_toml_path: Path) -> dict[str, dict[str, object]]:
    if not apps_toml_path.exists():
        return {}
    with apps_toml_path.open("rb") as f:
        registry = tomllib.load(f)
    entry_by_name: dict[str, dict[str, object]] = {}
    for app_entry in registry.get("apps", []):
        name = app_entry.get("name")
        if name is not None:
            entry_by_name[name] = app_entry
    return entry_by_name


def _string_field(entry: dict[str, object] | None, key: str) -> str | None:
    if entry is None:
        return None
    value = entry.get(key)
    return value if isinstance(value, str) else None


def discover_apps(repo_root: Path, apps_toml_path: Path) -> list[AppRef]:
    """List every versionable app: a folder under system/apps, joined with the app registry."""
    entry_by_name = _read_registry_entries_by_app_name(apps_toml_path)
    apps: list[AppRef] = []
    apps_dir = repo_root / APPS_SUBDIRECTORY
    for entry in sorted(apps_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")) or entry.name == "__pycache__":
            continue
        service_name = _service_name_for_package_dir(entry.name)
        registry_entry = entry_by_name.get(service_name)
        apps.append(
            AppRef(
                name=service_name,
                package_dir=f"{APPS_SUBDIRECTORY}/{entry.name}",
                title=_humanize_app_name(service_name),
                program=_string_field(registry_entry, "program"),
                icon=_string_field(registry_entry, "icon"),
            )
        )
    return apps


def find_app_by_name(repo_root: Path, apps_toml_path: Path, app_name: str) -> AppRef:
    """Raises AppNotFoundError if no app folder matches the name."""
    for app in discover_apps(repo_root, apps_toml_path):
        if app.name == app_name:
            return app
    raise AppNotFoundError(app_name)


@pure
def build_version_nodes(commits: list[CommitRecord]) -> list[VersionNode]:
    """Derive the version tree from an app's commit list. One commit is one node.

    Each node's parent is the previous commit, unless a Versioning-Restored-From
    trailer redirects it to the restored-to version. Nodes not on the current
    lineage were set aside by some restore. Milestones are user-requested kinds
    (and pre-convention commits with no kind); harden commits hide behind
    "More versions".
    """
    if len(commits) == 0:
        return []
    shas_present = {commit.sha for commit in commits}

    # First pass: one node per commit, parents redirected through restore trailers.
    bare_nodes: list[VersionNode] = []
    for idx, commit in enumerate(commits):
        restored_from = commit.trailers.restored_from_sha
        restored_from_in_history = restored_from if restored_from in shas_present else None
        parent_sha = (
            restored_from_in_history
            if restored_from_in_history is not None
            else (commits[idx - 1].sha if idx > 0 else None)
        )
        kind = commit.trailers.kind
        request = commit.trailers.request
        ported_from = commit.trailers.ported_from_sha
        bare_nodes.append(
            VersionNode(
                sha=commit.sha,
                raw_title=request if request is not None else commit.subject,
                is_titled_by_request=request is not None,
                kind=kind,
                authored_at=commit.authored_at,
                parent_sha=parent_sha,
                restored_from_sha=restored_from_in_history,
                ported_from_sha=ported_from if ported_from in shas_present else None,
                is_milestone=kind is None or kind in MILESTONE_KINDS,
            )
        )

    # Second pass: walk back from the newest node to find the current lineage;
    # everything off it was set aside by some restore.
    node_by_sha = {node.sha: node for node in bare_nodes}
    lineage_shas: set[str] = set()
    cursor: VersionNode | None = bare_nodes[-1]
    while cursor is not None:
        lineage_shas.add(cursor.sha)
        cursor = node_by_sha.get(cursor.parent_sha) if cursor.parent_sha is not None else None
    return [
        node.model_copy_update(
            to_update(node.field_ref().is_current, node.sha == bare_nodes[-1].sha),
            to_update(node.field_ref().is_set_aside, node.sha not in lineage_shas),
        )
        for node in bare_nodes
    ]


def build_app_history(git_repo: GitRepoInterface, app: AppRef) -> AppHistory:
    commits = git_repo.read_commits_touching_path(app.package_dir)
    stats_by_sha = git_repo.read_change_stats_by_sha(app.package_dir)
    nodes = [
        node.model_copy_update(to_update(node.field_ref().change_stats, stats_by_sha.get(node.sha)))
        for node in build_version_nodes(commits)
    ]
    return AppHistory(app=app, nodes=tuple(nodes))


@pure
def relative_time_label(moment: datetime, now: datetime) -> str:
    """A coarse human label like '2 hours ago', matching how people remember versions."""
    elapsed = now.astimezone(timezone.utc) - moment.astimezone(timezone.utc)
    if elapsed < timedelta(minutes=1):
        return "just now"
    elif elapsed < timedelta(hours=1):
        minutes = int(elapsed.total_seconds() // 60)
        return f"{minutes} min ago"
    elif elapsed < timedelta(days=1):
        hours = int(elapsed.total_seconds() // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif elapsed < timedelta(days=30):
        days = elapsed.days
        return f"{days} day{'s' if days != 1 else ''} ago"
    else:
        months = elapsed.days // 30
        return f"{months} month{'s' if months != 1 else ''} ago"


@pure
def short_relative_time_label(moment: datetime, now: datetime) -> str:
    """The same age, abbreviated to fit the timeline's narrow right-hand column.

    Reads as '12 min' / '17 hrs' / '3 days' next to a row, where the surrounding
    day heading already supplies the 'ago'.
    """
    elapsed = now.astimezone(timezone.utc) - moment.astimezone(timezone.utc)
    if elapsed < timedelta(minutes=1):
        return "now"
    elif elapsed < timedelta(hours=1):
        return f"{int(elapsed.total_seconds() // 60)} min"
    elif elapsed < timedelta(days=1):
        hours = int(elapsed.total_seconds() // 3600)
        return f"{hours} hr{'s' if hours != 1 else ''}"
    elif elapsed < timedelta(days=30):
        return f"{elapsed.days} day{'s' if elapsed.days != 1 else ''}"
    else:
        return f"{elapsed.days // 30} mo"
