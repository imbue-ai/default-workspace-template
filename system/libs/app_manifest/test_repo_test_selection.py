"""The real tree against the test selection's mapping: CI runs this on every change, and a
workspace whenever the selector or its override file changes."""

from pathlib import Path

from app_manifest.selection import OVERRIDES_PATH
from app_manifest.selection import is_docs_path
from app_manifest.selection import load_repo_layout
from app_manifest.selection import select_tests

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_every_tracked_path_is_classified() -> None:
    layout = load_repo_layout(_REPO_ROOT)
    paths = sorted(path for path in layout.tracked_files if not is_docs_path(path))

    selection = select_tests(layout, paths, None)

    assert selection.unclassified == (), (
        f"these paths map to no suite; add a mapping for each to {OVERRIDES_PATH}: "
        f"{list(selection.unclassified)}"
    )


def test_every_suite_the_override_file_names_exists() -> None:
    overrides = load_repo_layout(_REPO_ROOT).overrides
    named = [
        *overrides.always_run,
        *(suite for consumer in overrides.consumer for suite in consumer.suites),
        *(integration.test for integration in overrides.integration),
    ]

    missing = [suite for suite in named if not (_REPO_ROOT / suite).exists()]

    assert missing == []
