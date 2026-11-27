"""Fetching the template's release tags into a staged checkout, from a local repository standing in for it."""

import subprocess
from pathlib import Path

import pytest

from workspace_update_control.installed_version import read_installed_ref
from workspace_update_control.release_tags import (
    RELEASE_TAG_FETCH_RETRY_SECONDS,
    fetch_release_tags,
)
from workspace_update_control.testing import (
    ScriptedToolAnswer,
    StagedUpdateWorkspace,
    epoch_at,
    stage_checkout_at,
    stage_template_upstream,
    write_parent_file,
)

_RELEASE = "minds-v0.9.0"
_NOW = epoch_at("2026-10-20T12:00:00+00:00")
_FETCH_RECORD = "release-tags-fetch.json"


def _upstream_root(staged: StagedUpdateWorkspace) -> Path:
    return staged.root.parent / "template-upstream"


def _stage_a_checkout_with_none_of_its_templates_tags(
    staged: StagedUpdateWorkspace,
) -> Path:
    """A checkout whose template holds the tag of the release it is at; where that template is."""
    stage_checkout_at(staged.root, ("the template at a release", ""))
    stage_template_upstream(staged.root, _upstream_root(staged), _RELEASE)
    return _upstream_root(staged)


def test_the_tags_of_the_template_parent_toml_names_are_fetched_and_name_the_checkout(
    staged: StagedUpdateWorkspace,
) -> None:
    upstream = _stage_a_checkout_with_none_of_its_templates_tags(staged)
    subprocess.run(
        ["git", "tag", "not-a-release"], cwd=upstream, check=True, capture_output=True
    )
    assert read_installed_ref(".") == ""

    assert fetch_release_tags(_NOW) is True

    assert read_installed_ref(".") == _RELEASE
    tags = subprocess.run(
        ["git", "tag", "--list"], check=True, capture_output=True, text=True
    ).stdout.split()
    assert tags == [_RELEASE]
    assert staged.read_state(_FETCH_RECORD) == {"schema": 1, "attempted_at": _NOW}
    assert any(
        line.endswith("fetched the template's release tags")
        for line in staged.dispatcher_log_lines()
    )


def test_a_checkout_at_the_root_of_which_parent_toml_sits_is_fetched_into_as_well(
    staged: StagedUpdateWorkspace,
) -> None:
    _stage_a_checkout_with_none_of_its_templates_tags(staged)
    (staged.root / "system" / "config" / "parent.toml").rename(
        staged.root / "parent.toml"
    )

    assert fetch_release_tags(_NOW) is True

    assert read_installed_ref(".") == _RELEASE


def test_the_upstream_remote_is_fetched_from_where_the_checkout_has_one(
    staged: StagedUpdateWorkspace,
) -> None:
    upstream = _stage_a_checkout_with_none_of_its_templates_tags(staged)
    write_parent_file(staged.root, str(staged.root.parent / "no-such-template"))
    subprocess.run(
        ["git", "remote", "add", "upstream", str(upstream)],
        check=True,
        capture_output=True,
    )

    assert fetch_release_tags(_NOW) is True

    assert read_installed_ref(".") == _RELEASE


def test_nothing_is_fetched_into_a_checkout_that_was_not_made_from_the_template(
    staged: StagedUpdateWorkspace,
) -> None:
    upstream = _stage_a_checkout_with_none_of_its_templates_tags(staged)
    (staged.root / "system" / "config" / "parent.toml").unlink()
    subprocess.run(
        ["git", "remote", "add", "upstream", str(upstream)],
        check=True,
        capture_output=True,
    )

    assert fetch_release_tags(_NOW) is False

    assert read_installed_ref(".") == ""
    assert staged.read_state(_FETCH_RECORD) is None


def test_nothing_is_fetched_into_a_shallow_checkout(
    staged: StagedUpdateWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_checkout_at(
        staged.root, ("an earlier commit", ""), ("the template at a release", "")
    )
    stage_template_upstream(staged.root, _upstream_root(staged), _RELEASE)
    shallow_checkout = staged.root.parent / "shallow-checkout"
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-tags",
            "--depth",
            "1",
            _upstream_root(staged).as_uri(),
            str(shallow_checkout),
        ],
        check=True,
        capture_output=True,
    )
    write_parent_file(shallow_checkout, str(_upstream_root(staged)))
    monkeypatch.chdir(shallow_checkout)

    assert fetch_release_tags(_NOW) is False

    assert read_installed_ref(".") == ""
    assert not (shallow_checkout / "data").exists()


def test_a_fetch_that_fails_is_tried_again_a_day_later_and_no_sooner(
    staged: StagedUpdateWorkspace,
) -> None:
    stage_checkout_at(staged.root, ("the template at a release", ""))
    write_parent_file(staged.root, str(staged.root.parent / "no-such-template"))

    assert fetch_release_tags(_NOW) is True

    assert staged.read_state(_FETCH_RECORD) == {"schema": 1, "attempted_at": _NOW}
    failures = [
        line
        for line in staged.dispatcher_log_lines()
        if "could not fetch the template's release tags: " in line
    ]
    assert len(failures) == 1

    # The template is there by the next hour, and the day is not out.
    stage_template_upstream(
        staged.root, staged.root.parent / "no-such-template", _RELEASE
    )
    assert fetch_release_tags(_NOW + 60 * 60) is False
    assert read_installed_ref(".") == ""

    assert fetch_release_tags(_NOW + RELEASE_TAG_FETCH_RETRY_SECONDS) is True
    assert read_installed_ref(".") == _RELEASE


def test_an_attempt_recorded_in_the_future_does_not_put_the_next_one_off(
    staged: StagedUpdateWorkspace,
) -> None:
    _stage_a_checkout_with_none_of_its_templates_tags(staged)
    staged.write_state(_FETCH_RECORD, {"schema": 1, "attempted_at": _NOW + 3600})

    assert fetch_release_tags(_NOW) is True

    assert read_installed_ref(".") == _RELEASE


def test_a_parent_toml_that_names_no_template_fetches_nothing(
    staged: StagedUpdateWorkspace,
) -> None:
    stage_checkout_at(staged.root, ("the template at a release", ""))
    parent_file = staged.root / "system" / "config" / "parent.toml"
    parent_file.parent.mkdir(parents=True)
    parent_file.write_text('branch = "main"\n')

    assert fetch_release_tags(_NOW) is False

    assert staged.read_state(_FETCH_RECORD) is None


def test_a_failed_fetch_is_logged_without_the_credentials_its_address_holds(
    staged: StagedUpdateWorkspace,
) -> None:
    write_parent_file(staged.root, "https://example.com/template.git")
    address = "https://a-user:a-secret-token@example.com/private/template.git"
    # A checkout that is not shallow, whose ``upstream`` remote holds a token, and whose fetch git refuses.
    staged.script_program_on_path(
        "git",
        [
            ScriptedToolAnswer(stdout="false\n"),
            ScriptedToolAnswer(stdout=address + "\n"),
            ScriptedToolAnswer(
                exit_code=128,
                stderr=f"fatal: unable to access '{address}/': The requested URL returned error: 403\n",
            ),
        ],
    )

    assert fetch_release_tags(_NOW) is True

    fetch_argv = staged.tool_calls("git")[-1]
    assert fetch_argv[0] == "fetch" and address in fetch_argv
    failures = [
        line
        for line in staged.dispatcher_log_lines()
        if "could not fetch the template's release tags: " in line
    ]
    assert len(failures) == 1
    assert "'https://example.com/private/template.git/'" in failures[0]
    assert "a-secret-token" not in failures[0]
    assert "a-user" not in failures[0]
