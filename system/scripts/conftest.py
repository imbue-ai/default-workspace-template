"""Shared fixtures for the tests of the scripts in this directory."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hermetic_changelog_base_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the runner's diff-base environment out of these tests.

    ``check_changelog_entries.resolve_diff_base`` reads ``CHANGELOG_BASE_REF``
    and ``GITHUB_BASE_REF`` from the process environment, and its tests run it
    against throwaway repos built in ``tmp_path``. Under CI those variables name
    the *real* PR's base branch, which does not exist in a throwaway repo -- and
    since the function deliberately raises rather than falling back to ``main``
    for an unresolvable named base, the tests fail on the environment rather
    than on anything they assert.

    It only bites on a stacked PR: when the base is ``main``, the throwaway repo
    happens to have a ``main`` too, so the leak resolves by coincidence and the
    tests pass for the wrong reason. Clearing both makes every test start from
    the fallback path; the ones that exercise a named base set their own value,
    which still wins because that happens inside the test body.
    """
    monkeypatch.delenv("CHANGELOG_BASE_REF", raising=False)
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
