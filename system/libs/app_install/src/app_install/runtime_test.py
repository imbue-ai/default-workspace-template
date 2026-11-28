import os
from pathlib import Path

import pytest

from app_install.errors import DevRepositoryError
from app_install.errors import PublishedNameTakenError
from app_install.errors import PublishError
from app_install.runtime import WorkspaceRuntime
from app_install.runtime import has_test_files
from app_install.testing import write_workspace_file
from github_sync.testing import install_fake_latchkey

# A stand-in for latchkey that logs each request and answers GitHub's three calls of a publish: the creation
# with the content of the file beside it, the topics with the topic, and the settings with the repository.
_LATCHKEY_ANSWERING_A_PUBLISH = """\
directory="$(dirname "$0")"
printf '%s\\n' "$*" >> "$directory/requests"
case "$*" in
    *"-X POST https://api.github.com/user/repos"*) cat "$directory/creation.json" ;;
    *"-X PUT https://api.github.com/repos/octo/notes/topics"*) cat "$directory/topics.json" ;;
    *"-X PATCH https://api.github.com/repos/octo/notes"*) echo '{"name": "notes"}' ;;
    *) echo '{"message": "unexpected request"}' ;;
esac
"""


def _latchkey_answering_a_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, creation: str, topics: str) -> Path:
    """Put the stand-in latchkey first on PATH; the file it logs its requests to."""
    bin_directory = tmp_path / "bin"
    install_fake_latchkey(bin_directory, _LATCHKEY_ANSWERING_A_PUBLISH)
    (bin_directory / "creation.json").write_text(creation)
    (bin_directory / "topics.json").write_text(topics)
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")
    return bin_directory / "requests"


def test_a_published_repository_is_created_public_closed_to_outsiders_and_given_the_app_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _latchkey_answering_a_publish(
        tmp_path,
        monkeypatch,
        '{"name": "notes", "owner": {"login": "octo"}, "private": false}',
        '{"names": ["imbue-studio-app"]}',
    )

    created = WorkspaceRuntime(repo_root=tmp_path).create_published_repository(
        "notes", "Keep short notes", is_private=False
    )

    assert created.model_dump() == {
        "url": "https://github.com/octo/notes",
        "lockdown_problem": "",
        "topic_problem": "",
    }
    creation, topic, lockdown = requests.read_text().splitlines()
    assert '"private": false' in creation and '"description": "Keep short notes"' in creation
    assert '{"names": ["imbue-studio-app"]}' in topic
    assert '"has_issues": false' in lockdown and '"pull_request_creation_policy": "collaborators_only"' in lockdown


def test_a_published_repository_is_created_private_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _latchkey_answering_a_publish(
        tmp_path,
        monkeypatch,
        '{"name": "notes", "owner": {"login": "octo"}, "private": true}',
        '{"names": ["imbue-studio-app"]}',
    )

    WorkspaceRuntime(repo_root=tmp_path).create_published_repository("notes", "Keep short notes", is_private=True)

    assert '"private": true' in requests.read_text().splitlines()[0]


# A stand-in for latchkey that logs each request and answers what GitHub says of one repository: the content of
# the file beside it.
_LATCHKEY_ANSWERING_FOR_ONE_REPOSITORY = """\
directory="$(dirname "$0")"
printf '%s\\n' "$*" >> "$directory/requests"
cat "$directory/repository.json"
"""


def _latchkey_answering_for_one_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str) -> Path:
    """Put the stand-in latchkey first on PATH; the file it logs its requests to."""
    bin_directory = tmp_path / "bin"
    install_fake_latchkey(bin_directory, _LATCHKEY_ANSWERING_FOR_ONE_REPOSITORY)
    (bin_directory / "repository.json").write_text(answer)
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")
    return bin_directory / "requests"


def test_a_published_repository_is_made_public_or_the_refusal_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _latchkey_answering_for_one_repository(tmp_path, monkeypatch, '{"name": "notes", "private": false}')
    runtime = WorkspaceRuntime(repo_root=tmp_path)

    runtime.make_published_repository_public("https://github.com/octo/notes")

    (request,) = requests.read_text().splitlines()
    assert "-X PATCH https://api.github.com/repos/octo/notes" in request and '{"private": false}' in request

    (tmp_path / "bin" / "repository.json").write_text('{"error": "request not permitted by the user"}')
    with pytest.raises(PublishError, match="octo/notes was not made public"):
        runtime.make_published_repository_public("https://github.com/octo/notes")


def test_a_dev_repository_is_created_private_and_closed_to_outsiders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _latchkey_answering_a_publish(
        tmp_path, monkeypatch, '{"name": "notes", "owner": {"login": "octo"}, "private": true}', "{}"
    )
    runtime = WorkspaceRuntime(repo_root=tmp_path)

    created = runtime.create_private_repository("notes", "Private development history of the notes app")

    assert created.model_dump() == {
        "url": "https://github.com/octo/notes",
        "lockdown_problem": "",
        "topic_problem": "",
    }
    creation, lockdown = requests.read_text().splitlines()
    assert '"private": true' in creation and '"name": "notes"' in creation
    assert '"has_issues": false' in lockdown and '"pull_request_creation_policy": "collaborators_only"' in lockdown

    (tmp_path / "bin" / "creation.json").write_text('{"errors": [{"message": "name already exists on this account"}]}')
    with pytest.raises(DevRepositoryError, match="name already exists"):
        runtime.create_private_repository("notes", "Private development history of the notes app")


def test_a_topic_that_is_not_set_is_a_problem_of_a_repository_that_was_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _latchkey_answering_a_publish(
        tmp_path,
        monkeypatch,
        '{"name": "notes", "owner": {"login": "octo"}, "private": false}',
        '{"message": "Not Found"}',
    )

    created = WorkspaceRuntime(repo_root=tmp_path).create_published_repository(
        "notes", "Keep short notes", is_private=False
    )

    assert created.url == "https://github.com/octo/notes"
    assert "the topic was not set" in created.topic_problem


def test_a_name_the_account_already_uses_is_told_apart_from_every_other_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _latchkey_answering_a_publish(
        tmp_path, monkeypatch, '{"errors": [{"message": "name already exists on this account"}]}', "{}"
    )
    runtime = WorkspaceRuntime(repo_root=tmp_path)

    with pytest.raises(PublishedNameTakenError, match="name already exists"):
        runtime.create_published_repository("notes", "Keep short notes", is_private=False)
    # Nothing is asked of a repository that was not created.
    assert len(requests.read_text().splitlines()) == 1

    (tmp_path / "bin" / "creation.json").write_text('{"error": "request not permitted by the user"}')
    with pytest.raises(PublishError, match="github-rest-api") as refused:
        runtime.create_published_repository("notes", "Keep short notes", is_private=False)
    assert not isinstance(refused.value, PublishedNameTakenError)


@pytest.mark.parametrize("test_file", ["src/notes/server_test.py", "test_notes_ratchets.py"])
def test_an_app_with_a_test_file_of_its_own_has_tests_to_run(tmp_path: Path, test_file: str) -> None:
    write_workspace_file(tmp_path, "src/notes/server.py")
    write_workspace_file(tmp_path, test_file)

    assert has_test_files(tmp_path)


def test_the_tests_of_the_packages_an_app_installs_are_not_its_own(tmp_path: Path) -> None:
    write_workspace_file(tmp_path, "src/notes/server.py")
    write_workspace_file(tmp_path, ".venv/lib/python3.12/site-packages/flask/test_flask.py")
    write_workspace_file(tmp_path, "frontend/node_modules/some-package/helper_test.py")

    assert not has_test_files(tmp_path)
