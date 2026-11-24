"""Starting the cleanup helper: which chat it lands in, what it is told, and what happens when the app cannot answer."""

import subprocess

import pytest
from workspace_layout.errors import ShellRefusedOpError

from disk_space import helper, store
from disk_space.testing import FakeShell, FakeWorkspaceCommands


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch) -> FakeWorkspaceCommands:
    commands = FakeWorkspaceCommands()
    monkeypatch.setattr(helper, "_run", commands.run)
    return commands


@pytest.fixture
def shell(monkeypatch: pytest.MonkeyPatch) -> FakeShell:
    fake = FakeShell()
    monkeypatch.setattr(helper, "_shell", lambda: fake)
    return fake


def test_the_first_request_makes_a_chat_that_opens_and_runs_the_skill(
    workspace: FakeWorkspaceCommands, shell: FakeShell
) -> None:
    started = helper.start("The disk is 93% full.", "check")
    assert started == {"chat_id": "chat-abc", "created": True, "detail": ""}
    created = workspace.creates()[0]
    assert created[:4] == ["python3", helper.MESSAGE_CHAT, "--create", "--name"]
    assert created[4] == "Free up disk space"
    assert "auto_open=true" in created
    assert created[-1].startswith("/free-disk-space")
    assert "The disk is 93% full." in created[-1]
    assert store.read_watch()["helper_chat_id"] == "chat-abc"
    assert store.read_history()[0]["summary"] == "Started the cleanup helper (check)."
    # The routine needs no window of its own: the chat's own label opens it.
    assert shell.shown == []


def test_a_second_request_goes_to_the_chat_already_open(
    workspace: FakeWorkspaceCommands, shell: FakeShell
) -> None:
    store.update_watch(helper_chat_id="chat-existing")
    workspace.queue(0)
    started = helper.start("The disk is 94% full.", "page", item="/var/tmp/leftover")
    assert started == {"chat_id": "chat-existing", "created": False, "detail": ""}
    assert workspace.creates() == []
    sent = workspace.messages()[0]
    assert sent[1:3] == [helper.MESSAGE_CHAT, "chat-existing"]
    assert "Start with this item the user asked about: /var/tmp/leftover" in sent[-1]
    assert len(shell.shown) == 1
    assert str(shell.shown[0].path) == "/?chat=chat-existing"
    assert [str(one) for one in shell.shown[0].showing] == ["/chat-existing"]
    assert [str(one) for one in shell.shown[0].repoint] == ["/"]
    assert (
        store.read_history()[0]["summary"] == "Asked the cleanup helper again (page)."
    )


def test_a_request_from_the_routine_does_not_put_the_window_on_screen(
    workspace: FakeWorkspaceCommands, shell: FakeShell
) -> None:
    store.update_watch(helper_chat_id="chat-existing")
    workspace.queue(0)
    helper.start("The disk is 94% full.", "check")
    assert shell.shown == []


def test_a_chat_whose_message_landed_behind_a_dialog_still_counts_as_reused(
    workspace: FakeWorkspaceCommands, shell: FakeShell
) -> None:
    store.update_watch(helper_chat_id="chat-existing")
    workspace.queue(7, stderr="delivered but blocked")
    assert helper.start("The disk is 95% full.", "check")["created"] is False
    assert workspace.creates() == []


def test_a_vanished_helper_chat_is_replaced_with_a_new_one(
    workspace: FakeWorkspaceCommands,
) -> None:
    store.update_watch(helper_chat_id="chat-gone")
    workspace.queue(8, stderr="the chat is gone")
    workspace.queue_chat("chat-new")
    started = helper.start("The disk is 95% full.", "check")
    assert started == {"chat_id": "chat-new", "created": True, "detail": ""}
    assert len(workspace.creates()) == 1
    assert store.read_watch()["helper_chat_id"] == "chat-new"


def test_a_name_already_taken_is_tried_again_with_another(
    workspace: FakeWorkspaceCommands,
) -> None:
    workspace.queue(1, stderr="the name Free up disk space is taken\n")
    workspace.queue_chat("chat-second")
    started = helper.start("The disk is 95% full.", "check")
    names = [call[4] for call in workspace.creates()]
    assert names == ["Free up disk space", "Free up disk space 2"]
    assert started["chat_id"] == "chat-second"


def test_a_chat_app_that_keeps_refusing_is_reported_with_its_own_words(
    workspace: FakeWorkspaceCommands,
) -> None:
    workspace.queue(1, stderr="no account is connected\n")
    started = helper.start("The disk is 95% full.", "check")
    assert started == {
        "chat_id": "",
        "created": False,
        "detail": "no account is connected",
    }
    assert store.read_watch().get("helper_chat_id") is None
    assert store.read_history()[0]["kind"] == "helper-failed"


def test_a_chat_app_that_never_answers_is_reported_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never_answers(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, helper.CREATE_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", never_answers)
    started = helper.start("The disk is 95% full.", "check")
    assert started["chat_id"] == ""
    assert "did not answer" in started["detail"]
    assert store.read_history()[0]["kind"] == "helper-failed"


def test_a_chat_command_that_cannot_run_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cannot_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(subprocess, "run", cannot_run)
    started = helper.start("The disk is 95% full.", "check")
    assert started["chat_id"] == ""
    assert "could not be run" in started["detail"]


def test_a_chat_made_without_saying_which_one_is_a_failure(
    workspace: FakeWorkspaceCommands,
) -> None:
    workspace.queue(0, stdout="not json\n")
    started = helper.start("The disk is 95% full.", "check")
    assert started == {
        "chat_id": "",
        "created": False,
        "detail": "The chat app made a chat but did not say which one.",
    }
    assert store.read_watch().get("helper_chat_id") is None
    assert store.read_history()[0]["kind"] == "helper-failed"


def test_a_shell_that_will_not_show_the_window_does_not_lose_the_chat(
    workspace: FakeWorkspaceCommands, shell: FakeShell
) -> None:
    store.update_watch(helper_chat_id="chat-existing")
    workspace.queue(0)
    shell.refusal = ShellRefusedOpError(
        "the shell has no such desktop", status_code=400, detail="no such desktop"
    )
    started = helper.start("The disk is 94% full.", "page")
    assert started == {
        "chat_id": "chat-existing",
        "created": False,
        "detail": "",
    }
    assert (
        store.read_history()[0]["summary"] == "Asked the cleanup helper again (page)."
    )
