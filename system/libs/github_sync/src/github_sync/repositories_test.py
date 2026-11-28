"""Creating and closing a repository, against a stand-in `latchkey`."""

import json
from pathlib import Path

import pytest

from github_sync.repositories import (
    GithubRequestError,
    RepositoryNameTakenError,
    create_repository,
    hint_for_push_failure,
    lock_down_repository,
    make_repository_public,
    set_repository_topics,
)
from github_sync.testing import install_fake_latchkey


def _answer(bin_dir: Path, body: dict[str, object], log: Path) -> None:
    """Have `latchkey` answer every request with `body`, logging its arguments."""
    answer = bin_dir / "answer.json"
    answer.write_text(json.dumps(body))
    install_fake_latchkey(bin_dir, f'printf "%s\\n" "$@" >> "{log}"\ncat "{answer}"')


def test_a_repository_is_created_private_under_the_authenticated_account(
    fake_latchkey_bin: Path, tmp_path: Path
) -> None:
    log = tmp_path / "latchkey-arguments"
    _answer(
        fake_latchkey_bin,
        {"name": "notes-dev", "owner": {"login": "octo"}, "private": True},
        log,
    )

    repository = create_repository("notes-dev", 'The "notes" app', is_private=True)

    assert repository == "octo/notes-dev"
    arguments = log.read_text().splitlines()
    assert arguments[:5] == [
        "curl",
        "-s",
        "-X",
        "POST",
        "https://api.github.com/user/repos",
    ]
    assert json.loads(arguments[-1]) == {
        "name": "notes-dev",
        "description": 'The "notes" app',
        "private": True,
    }


@pytest.mark.parametrize(
    ("answered", "is_private", "wanted"),
    [(False, True, "private"), (None, True, "private"), (True, False, "public")],
)
def test_a_repository_whose_visibility_github_does_not_confirm_is_a_refused_request(
    fake_latchkey_bin: Path, tmp_path: Path, answered: bool | None, is_private: bool, wanted: str
) -> None:
    body: dict[str, object] = {"name": "notes", "owner": {"login": "octo"}}
    if answered is not None:
        body["private"] = answered
    _answer(fake_latchkey_bin, body, tmp_path / "log")

    with pytest.raises(GithubRequestError, match=f"octo/notes was created, but GitHub did not confirm it is {wanted}"):
        create_repository("notes", "notes", is_private=is_private)


def test_a_taken_name_is_told_apart_from_every_other_refusal(
    fake_latchkey_bin: Path, tmp_path: Path
) -> None:
    _answer(
        fake_latchkey_bin,
        {
            "message": "Repository creation failed.",
            "errors": [{"message": "name already exists on this account"}],
        },
        tmp_path / "log",
    )
    with pytest.raises(RepositoryNameTakenError, match="name already exists"):
        create_repository("notes-dev", "notes", is_private=True)

    _answer(
        fake_latchkey_bin,
        {"error": "request not permitted by the user"},
        tmp_path / "log",
    )
    with pytest.raises(GithubRequestError, match="github-rest-api") as refused:
        create_repository("notes-dev", "notes", is_private=True)
    assert not isinstance(refused.value, RepositoryNameTakenError)


def test_a_latchkey_that_fails_or_answers_no_json_is_a_refused_request(
    fake_latchkey_bin: Path,
) -> None:
    install_fake_latchkey(fake_latchkey_bin, "echo 'gateway offline' >&2; exit 7")
    with pytest.raises(GithubRequestError, match="gateway offline"):
        create_repository("notes-dev", "notes", is_private=True)

    install_fake_latchkey(fake_latchkey_bin, "echo 'not json'")
    with pytest.raises(GithubRequestError, match="not json"):
        create_repository("notes-dev", "notes", is_private=True)


def test_locking_down_closes_every_surface_and_reports_what_did_not_take(
    fake_latchkey_bin: Path, tmp_path: Path
) -> None:
    log = tmp_path / "latchkey-arguments"
    _answer(fake_latchkey_bin, {"name": "notes-dev"}, log)

    assert lock_down_repository("octo/notes-dev") is None

    arguments = log.read_text().splitlines()
    assert arguments[3:5] == ["PATCH", "https://api.github.com/repos/octo/notes-dev"]
    assert json.loads(arguments[-1]) == {
        "pull_request_creation_policy": "collaborators_only",
        "has_issues": False,
        "has_wiki": False,
        "has_projects": False,
        "has_discussions": False,
    }

    _answer(fake_latchkey_bin, {"message": "Server Error"}, log)
    assert lock_down_repository("octo/notes-dev") == '{"message": "Server Error"}'


def test_topics_are_set_and_a_refusal_is_raised(
    fake_latchkey_bin: Path, tmp_path: Path
) -> None:
    log = tmp_path / "latchkey-arguments"
    _answer(fake_latchkey_bin, {"names": ["imbue-studio-app"]}, log)

    set_repository_topics("octo/notes", ["imbue-studio-app"])

    arguments = log.read_text().splitlines()
    assert arguments[3:5] == ["PUT", "https://api.github.com/repos/octo/notes/topics"]
    assert json.loads(arguments[-1]) == {"names": ["imbue-studio-app"]}

    _answer(fake_latchkey_bin, {"message": "Not Found"}, log)
    with pytest.raises(GithubRequestError, match="the topic was not set"):
        set_repository_topics("octo/notes", ["imbue-studio-app"])


def test_a_repository_is_made_public_and_one_that_stays_private_is_raised(
    fake_latchkey_bin: Path, tmp_path: Path
) -> None:
    log = tmp_path / "latchkey-arguments"
    _answer(fake_latchkey_bin, {"name": "notes", "private": False}, log)

    make_repository_public("octo/notes")

    arguments = log.read_text().splitlines()
    assert arguments[:5] == ["curl", "-s", "-X", "PATCH", "https://api.github.com/repos/octo/notes"]
    assert json.loads(arguments[-1]) == {"private": False}
    _answer(fake_latchkey_bin, {"name": "notes", "private": True}, log)
    with pytest.raises(GithubRequestError, match="octo/notes was not made public"):
        make_repository_public("octo/notes")


def test_a_refused_push_is_explained_only_by_what_git_itself_said() -> None:
    refused = (
        " ! [rejected]        abc123 -> main (non-fast-forward)\n"
        "error: failed to push some refs to 'https://github.com/octo/notes.git'"
    )
    hint = hint_for_push_failure(refused)
    assert hint is not None and "never force-push" in hint

    # The remote's URL is in git's output too, so a repository's name must never read as a cause.
    assert (
        hint_for_push_failure(
            "fatal: unable to access 'https://github.com/octo/non-fast-forward.git/': "
            "Could not resolve host: github.com"
        )
        is None
    )
