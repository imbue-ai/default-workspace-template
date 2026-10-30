"""Every user package under ``system/`` is a standalone uv project with a current lock.

A user package is a directory under ``system/apps``, ``system/services`` or
``system/libs`` with a ``pyproject.toml`` that the root ``members`` does not name. One
that still leans on the root workspace -- no lock of its own, a ``{ workspace = true }``
source, or a program running it from the root venv -- shares the built-ins' versions, so
a dependency it adds can move the chat's. An older workspace's packages fail here until
they are converted; the message says how.
"""

from pathlib import Path

from workspace_bare_scripts import user_packages

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOW_TO_FIX = (
    f"Convert it to a standalone project as {user_packages.MIGRATION_REFERENCE} says, "
    "under 'A user app, service or lib' (start with the migration script's --dry-run). "
    "Do not add a user package to the root pyproject's members."
)


def test_every_user_package_is_standalone_and_its_lock_is_current() -> None:
    failures: list[str] = []
    for relative in user_packages.user_packages(_REPO_ROOT):
        problems = user_packages.standalone_problems(_REPO_ROOT, relative)
        if not problems:
            stale = user_packages.lock_check(_REPO_ROOT, relative)
            if stale is not None:
                problems.append(
                    f"its uv.lock is out of date with its pyproject.toml ({stale}); relock it "
                    "with the migration script's `relock`"
                )
        if problems:
            failures.append(
                f"{relative} is not a standalone project: " + "; ".join(problems)
            )
    assert not failures, "\n".join(failures) + "\n" + _HOW_TO_FIX
