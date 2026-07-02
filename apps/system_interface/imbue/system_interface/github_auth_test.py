"""Unit tests for the GitHub-auth helpers that touch the process environment.

`_gh_child_env` is the load-bearing fix for the in-mind GitHub login modal:
`gh` prioritizes `GH_TOKEN` / `GITHUB_TOKEN` (and enterprise variants) over its
credential store, and the system_interface process inherits `GH_TOKEN` from the
agent environment. If those are left in the child environment, `gh auth login`
refuses to persist a new credential and `gh auth status` reports the env token
instead of the store, so the modal can never write a durable credential. These
tests pin that every such variable is stripped from the child environment while
the parent process environment is left untouched.
"""

from __future__ import annotations

import os

import pytest

from imbue.system_interface.github_auth import _GH_TOKEN_ENV_VARS
from imbue.system_interface.github_auth import _gh_child_env


@pytest.mark.parametrize("token_var", _GH_TOKEN_ENV_VARS)
def test_gh_child_env_strips_each_github_token_var(token_var: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each GitHub token variable is removed from the child environment."""
    monkeypatch.setenv(token_var, "ghp_shadowing_value")
    child_env = _gh_child_env()
    assert token_var not in child_env


def test_gh_child_env_preserves_unrelated_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-token variables survive the scrub so gh keeps its normal environment."""
    monkeypatch.setenv("GH_TOKEN", "ghp_shadowing_value")
    monkeypatch.setenv("PATH_MARKER_FOR_TEST", "keep-me")
    child_env = _gh_child_env()
    assert child_env["PATH_MARKER_FOR_TEST"] == "keep-me"
    assert "GH_TOKEN" not in child_env


def test_gh_child_env_does_not_mutate_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrubbing is child-only: os.environ still holds the token afterwards."""
    monkeypatch.setenv("GH_TOKEN", "ghp_shadowing_value")
    _gh_child_env()
    assert os.environ["GH_TOKEN"] == "ghp_shadowing_value"


def test_gh_child_env_overrides_apply_after_scrub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller overrides are layered onto the scrubbed base environment."""
    monkeypatch.setenv("GH_TOKEN", "ghp_shadowing_value")
    child_env = _gh_child_env({"EXTRA_VAR_FOR_TEST": "value"})
    assert child_env["EXTRA_VAR_FOR_TEST"] == "value"
    assert "GH_TOKEN" not in child_env
