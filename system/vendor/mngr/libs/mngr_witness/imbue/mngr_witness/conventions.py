"""The repository's layout rules the gates and prompts share: what counts as a test path, a changelog entry, a project."""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from imbue.imbue_common.pure import pure

_TEST_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset({"testing", "witnesses"})
_TEST_FILE_NAMES: Final[frozenset[str]] = frozenset({"conftest.py", "testing.py"})
_CHANGELOG_DIRECTORY_NAME: Final[str] = "changelog"
_PROJECT_ROOT_DIRECTORIES: Final[frozenset[str]] = frozenset({"libs", "apps"})
_ROOT_PROJECT: Final[str] = "dev"


@pure
def is_test_path(path: str) -> bool:
    """Whether a repo-relative path is one the test-only commit kinds may touch."""
    parts = Path(path).parts
    name = parts[-1]
    if any(part in _TEST_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    if name in _TEST_FILE_NAMES:
        return True
    return name.endswith("_test.py") or (name.startswith("test_") and name.endswith(".py"))


@pure
def is_changelog_path(path: str) -> bool:
    return _CHANGELOG_DIRECTORY_NAME in Path(path).parts[:-1]


@pure
def project_of_path(path: str) -> str:
    """``libs/x`` or ``apps/x`` for a path inside a project, else the root-level ``dev`` project."""
    parts = Path(path).parts
    if len(parts) > 2 and parts[0] in _PROJECT_ROOT_DIRECTORIES:
        return f"{parts[0]}/{parts[1]}"
    return _ROOT_PROJECT


@pure
def changelog_entry_path(project: str, changelog_branch: str) -> str:
    return f"{project}/{_CHANGELOG_DIRECTORY_NAME}/{changelog_branch.replace('/', '-')}.md"


@pure
def changelog_problems(touched_paths: Sequence[str], changelog_branch: str) -> list[str]:
    """Why the touched paths break the one-entry-per-project rule, empty when they obey."""
    non_changelog_paths = [path for path in touched_paths if not is_changelog_path(path)]
    expected = {changelog_entry_path(project_of_path(path), changelog_branch) for path in non_changelog_paths}
    actual = {path for path in touched_paths if is_changelog_path(path)}
    problems = [f"missing changelog entry {path}" for path in sorted(expected - actual)]
    problems.extend(f"unexpected changelog entry {path}" for path in sorted(actual - expected))
    return problems
