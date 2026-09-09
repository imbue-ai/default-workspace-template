"""Tests for the update notice: reading the kept rollback point, announcing its changes, and the verbs that close it."""

import os
from pathlib import Path

import pytest
from app_instances.testing import wait_until

from imbue.system_interface.shell.errors import UpdateNoticeCommandError
from imbue.system_interface.shell.errors import UpdateNoticeRefusedError
from imbue.system_interface.shell.testing import drain_messages
from imbue.system_interface.shell.testing import read_stub_update_self_calls
from imbue.system_interface.shell.testing import write_rollback_point
from imbue.system_interface.shell.testing import write_stub_update_self_script
from imbue.system_interface.shell.update_notice import UpdateNoticeWatch
from imbue.system_interface.shell.update_notice import read_update_notice
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster


def _watch(tmp_path: Path) -> UpdateNoticeWatch:
    return UpdateNoticeWatch(repo_root=tmp_path / "repo", broadcaster=WebSocketBroadcaster())


def test_the_record_reads_as_a_notice_and_anything_else_as_none(tmp_path: Path) -> None:
    """The apply's record carries more than the notice shows (the rollback target, the copies); the notice
    takes what the windows need. No record, or one that is not a record, is no notice rather than an error."""
    watch = _watch(tmp_path)
    assert watch.current() is None

    path = write_rollback_point(
        watch.repo_root, apps=["chat", "system_interface"], programs=["chat", "system_interface"]
    )
    notice = watch.current()
    assert notice is not None
    assert notice.apps == ("chat", "system_interface")
    assert notice.programs == ("chat", "system_interface")
    assert notice.driven_by == "mngr/update-widgets"
    assert not notice.is_rolling_back and not notice.is_settled
    assert notice.wire_json()["merge_sha"] == "abc1234abc1234abc1234abc1234abc1234abc12"

    path.write_text("{not json")
    assert read_update_notice(path) is None
    # No merge sha: not a record.
    path.write_text('{"progress": "x"}')
    assert read_update_notice(path) is None


def test_a_rolling_back_record_and_a_settled_one_are_told_apart(tmp_path: Path) -> None:
    watch = _watch(tmp_path)
    write_rollback_point(watch.repo_root, progress="Restoring the previous version")
    running = watch.current()
    assert running is not None and running.is_rolling_back and not running.is_settled

    write_rollback_point(watch.repo_root, progress=None, outcome="Rolled back to the previous version.")
    settled = watch.current()
    assert settled is not None and settled.is_settled and not settled.is_rolling_back


@pytest.mark.timeout(30)
def test_the_watch_announces_each_distinct_reading_of_the_record(tmp_path: Path) -> None:
    """A record raised, rewritten with progress, and cleared reaches every registered window once each, as
    the reading it now has: the rollback script rewrites the file as it goes, and a window that sees the
    same reading twice would only redraw for nothing."""
    watch = _watch(tmp_path)
    client_queue = watch.broadcaster.register()
    watch.start()
    try:
        write_rollback_point(watch.repo_root, apps=["terminal"])
        assert wait_until(lambda: not client_queue.empty(), timeout_seconds=10)
        raised = drain_messages(client_queue)
        assert [message["type"] for message in raised] == ["update_notice_changed"]
        assert raised[0]["notice"]["apps"] == ["terminal"]

        write_rollback_point(watch.repo_root, apps=["terminal"], progress="Reverting the update")
        assert wait_until(lambda: not client_queue.empty(), timeout_seconds=10)
        progressed = drain_messages(client_queue)
        assert [message["notice"]["progress"] for message in progressed] == ["Reverting the update"]

        (watch.repo_root / "data/.state/update-apply/last-good.json").unlink()
        assert wait_until(lambda: not client_queue.empty(), timeout_seconds=10)
        cleared = drain_messages(client_queue)
        assert [message["notice"] for message in cleared] == [None]
    finally:
        watch.stop()


def test_confirm_runs_the_scripts_confirm_last_in_the_workspace(tmp_path: Path) -> None:
    """Closing the notice is the update-self script's verb, run from the workspace root so the script finds
    the record and the copies where the apply left them; the record is gone afterwards because the script
    removed it, not the shell."""
    watch = _watch(tmp_path)
    write_stub_update_self_script(watch.repo_root)
    write_rollback_point(watch.repo_root)

    watch.confirm()

    calls = read_stub_update_self_calls(watch.repo_root)
    assert [call["argv"] for call in calls] == [["confirm-last"]]
    assert Path(calls[0]["cwd"]).resolve() == watch.repo_root.resolve()
    assert watch.current() is None


def test_confirm_is_refused_without_a_record_and_while_a_rollback_runs(tmp_path: Path) -> None:
    """Confirming discards the copies, so a rollback in flight is restoring from what it would take
    away -- and a settled record is exactly what the notice's Close button confirms."""
    watch = _watch(tmp_path)
    write_stub_update_self_script(watch.repo_root)
    with pytest.raises(UpdateNoticeRefusedError, match="no update notice"):
        watch.confirm()

    write_rollback_point(watch.repo_root, progress="Restoring the previous version")
    with pytest.raises(UpdateNoticeRefusedError, match="rollback is running"):
        watch.confirm()
    assert read_stub_update_self_calls(watch.repo_root) == []

    write_rollback_point(watch.repo_root, outcome="Rolled back to the previous version.")
    watch.confirm()
    assert [call["argv"] for call in read_stub_update_self_calls(watch.repo_root)] == [["confirm-last"]]


def test_confirm_reports_a_failing_script(tmp_path: Path) -> None:
    watch = _watch(tmp_path)
    write_stub_update_self_script(watch.repo_root, exit_code=1)

    write_rollback_point(watch.repo_root)
    with pytest.raises(UpdateNoticeCommandError, match=r"exit 1[\s\S]*told to fail"):
        watch.confirm()
    # The failed script left the record, so the notice still stands.
    assert watch.current() is not None


@pytest.mark.timeout(30)
def test_the_rollback_launches_the_script_detached_in_its_own_session(tmp_path: Path) -> None:
    """The launch answers before the script runs, and the script is in a session of its own: it restarts the
    shell's own supervisord program group when the shell was touched, which would take a child of this
    process down with it."""
    watch = _watch(tmp_path)
    write_stub_update_self_script(watch.repo_root)
    write_rollback_point(watch.repo_root, apps=["system_interface"])

    watch.launch_rollback()

    assert wait_until(lambda: len(read_stub_update_self_calls(watch.repo_root)) == 1, timeout_seconds=10)
    (call,) = read_stub_update_self_calls(watch.repo_root)
    assert call["argv"] == ["rollback-last"]
    assert Path(call["cwd"]).resolve() == watch.repo_root.resolve()
    assert call["sid"] != os.getsid(0)


def test_the_rollback_is_refused_without_a_point_while_one_runs_and_once_it_settled(tmp_path: Path) -> None:
    watch = _watch(tmp_path)
    write_stub_update_self_script(watch.repo_root)
    with pytest.raises(UpdateNoticeRefusedError, match="no update to roll back"):
        watch.launch_rollback()

    write_rollback_point(watch.repo_root, progress="Restarting")
    with pytest.raises(UpdateNoticeRefusedError, match="already running"):
        watch.launch_rollback()

    write_rollback_point(watch.repo_root, outcome="Rolled back to the previous version.")
    with pytest.raises(UpdateNoticeRefusedError, match="already rolled back"):
        watch.launch_rollback()
    assert read_stub_update_self_calls(watch.repo_root) == []
