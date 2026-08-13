"""Shared fixtures for the tests of the scripts in this directory."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_github_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the runner's GitHub Actions environment out of these tests.

    ``check_changelog_entries`` reads the branch and the diff base from the
    process environment -- ``resolve_diff_base`` takes ``CHANGELOG_BASE_REF``
    then ``GITHUB_BASE_REF``, and ``detect_branch`` takes ``GITHUB_HEAD_REF``
    then ``GITHUB_REF_NAME`` -- while its tests run it against throwaway repos
    built in ``tmp_path``. Under CI those variables describe the *real* PR, so
    they answer questions about a repo the test never created.

    It surfaced through the base: a stacked PR's base branch does not exist in a
    throwaway repo, and ``resolve_diff_base`` deliberately raises rather than
    falling back to ``main`` for an unresolvable named base, so three tests
    failed on the environment rather than on anything they assert. When the base
    is ``main`` the throwaway repo happens to have a ``main`` too, so the leak
    had been resolving by coincidence and the tests passed for the wrong reason.

    All four are cleared, not just the two that bit: the branch pair is read by
    the same module, and this file's upstream ancestor
    (``system/vendor/mngr/scripts/check_changelog_entries_test.py``) already
    clears the GitHub trio for exactly this reason. Losing them in the derived
    copy is how the base leak got here.

    The tests that exercise a named base set their own value, which still wins
    because that happens inside the test body.
    """
    for var in (
        "CHANGELOG_BASE_REF",
        "GITHUB_BASE_REF",
        "GITHUB_HEAD_REF",
        "GITHUB_REF_NAME",
    ):
        monkeypatch.delenv(var, raising=False)
