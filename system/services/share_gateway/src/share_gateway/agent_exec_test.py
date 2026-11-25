import os
import signal
import socket
import stat
import sys
import threading
from pathlib import Path

import pytest

from share_gateway.agent_exec import DEFAULT_COMMAND_TIMEOUT_SECONDS
from share_gateway.agent_exec import DEFAULT_FILE_MODE
from share_gateway.agent_exec import InvalidAgentRequestError
from share_gateway.agent_exec import MAX_COMMAND_TIMEOUT_SECONDS
from share_gateway.agent_exec import MAX_READ_FILE_BYTES
from share_gateway.agent_exec import MIN_COMMAND_TIMEOUT_SECONDS
from share_gateway.agent_exec import RunRequest
from share_gateway.agent_exec import RunningCommands
from share_gateway.agent_exec import WorkspaceFileError
from share_gateway.agent_exec import _LineSplitter
from share_gateway.agent_exec import parse_file_mode
from share_gateway.agent_exec import parse_read_file_request
from share_gateway.agent_exec import parse_run_request
from share_gateway.agent_exec import parse_write_file_request
from share_gateway.agent_exec import read_workspace_file
from share_gateway.agent_exec import resolve_timeout_seconds
from share_gateway.agent_exec import resolve_workspace_path
from share_gateway.agent_exec import stream_command
from share_gateway.agent_exec import write_workspace_file
from share_gateway.testing import ProcessLifeline
from share_gateway.testing import run_to_completion
from share_gateway.testing import start_holding

_ROOT = Path("/home/user/workspace")
_LIFELINE_WAIT_SECONDS = 5.0


def test_a_path_is_taken_as_written_when_absolute_and_under_the_repository_root_otherwise() -> None:
    assert resolve_workspace_path(_ROOT, "data/notes.txt") == _ROOT / "data" / "notes.txt"
    assert resolve_workspace_path(_ROOT, "/etc/hostname") == Path("/etc/hostname")
    assert resolve_workspace_path(_ROOT, "") == _ROOT
    assert resolve_workspace_path(_ROOT, "data/../system/./x") == _ROOT / "system" / "x"
    assert resolve_workspace_path(_ROOT, "../elsewhere") == Path("/home/user/elsewhere")


def test_a_command_is_allowed_ten_minutes_unless_it_asks_and_never_more_than_an_hour() -> None:
    assert resolve_timeout_seconds(None) == DEFAULT_COMMAND_TIMEOUT_SECONDS == 600.0
    assert resolve_timeout_seconds(30) == 30.0
    assert resolve_timeout_seconds(2.5) == 2.5
    assert resolve_timeout_seconds(7200) == MAX_COMMAND_TIMEOUT_SECONDS == 3600.0
    assert resolve_timeout_seconds(0) == MIN_COMMAND_TIMEOUT_SECONDS == 1.0
    assert resolve_timeout_seconds(-5) == 1.0
    assert resolve_timeout_seconds(10**400) == 3600.0
    assert resolve_timeout_seconds(float("inf")) == 3600.0


@pytest.mark.parametrize("raw", ["30", True, [30], {"seconds": 30}, float("nan")])
def test_a_timeout_that_is_not_a_number_is_refused(raw: object) -> None:
    with pytest.raises(InvalidAgentRequestError, match="timeout_seconds must be a number of seconds"):
        resolve_timeout_seconds(raw)


def test_a_mode_is_read_as_octal_permission_bits_and_defaults_to_owner_only() -> None:
    assert parse_file_mode(None) == DEFAULT_FILE_MODE == 0o600
    assert parse_file_mode("") == 0o600
    assert parse_file_mode("644") == 0o644
    assert parse_file_mode("0755") == 0o755
    assert parse_file_mode("7") == 0o007


@pytest.mark.parametrize("raw", ["rw-r--r--", "888", "4755", "0o644", "-644", " 644", 644, True])
def test_a_mode_that_is_not_octal_permission_bits_is_refused(raw: object) -> None:
    with pytest.raises(InvalidAgentRequestError, match="mode must be the octal digits"):
        parse_file_mode(raw)


def test_a_run_request_names_its_words_its_directory_and_its_time() -> None:
    bare = parse_run_request({"command": ["ls", "-la"]}, _ROOT)
    assert bare.command == ["ls", "-la"] and bare.cwd == _ROOT and bare.timeout_seconds == 600.0
    full = parse_run_request({"command": ["make"], "cwd": "system/apps", "timeout_seconds": 90}, _ROOT)
    assert full.cwd == _ROOT / "system" / "apps" and full.timeout_seconds == 90.0
    assert parse_run_request({"command": ["ls"], "cwd": "/var/tmp"}, _ROOT).cwd == Path("/var/tmp")
    assert parse_run_request({"command": ["ls"], "cwd": None, "timeout_seconds": None}, _ROOT).cwd == _ROOT
    assert parse_run_request({"command": ["ls"], "unknown": 1}, _ROOT).command == ["ls"]


@pytest.mark.parametrize("command", [None, [], "ls -la", ["ls", 1], [["ls"]], {"0": "ls"}])
def test_a_run_request_without_a_list_of_words_is_refused(command: object) -> None:
    with pytest.raises(InvalidAgentRequestError, match="command must be a non-empty list of strings"):
        parse_run_request({"command": command}, _ROOT)


def test_a_run_request_whose_directory_is_not_a_string_is_refused() -> None:
    with pytest.raises(InvalidAgentRequestError, match="cwd must be a string"):
        parse_run_request({"command": ["ls"], "cwd": 5}, _ROOT)


@pytest.mark.parametrize("path", [None, "", 5, ["a"]])
def test_a_file_request_without_a_path_is_refused(path: object) -> None:
    with pytest.raises(InvalidAgentRequestError, match="path must be a non-empty string"):
        parse_read_file_request({"path": path}, _ROOT)
    with pytest.raises(InvalidAgentRequestError, match="path must be a non-empty string"):
        parse_write_file_request({"path": path, "content_b64": ""}, _ROOT)


@pytest.mark.parametrize("path", ["data/a\x00b", "data/\ud800"])
def test_a_file_request_whose_path_no_file_could_have_is_refused(path: str) -> None:
    with pytest.raises(InvalidAgentRequestError, match="path is not one a file can have"):
        parse_read_file_request({"path": path}, _ROOT)
    with pytest.raises(InvalidAgentRequestError, match="path is not one a file can have"):
        parse_write_file_request({"path": path, "content_b64": ""}, _ROOT)


def test_a_write_request_carries_decoded_bytes_and_a_mode() -> None:
    request = parse_write_file_request({"path": "data/a.bin", "content_b64": "AAEC/w==", "mode": "640"}, _ROOT)
    assert request.raw_path == "data/a.bin" and request.path == _ROOT / "data" / "a.bin"
    assert request.content == b"\x00\x01\x02\xff" and request.mode == 0o640
    empty = parse_write_file_request({"path": "data/empty", "content_b64": ""}, _ROOT)
    assert empty.content == b"" and empty.mode == 0o600


def test_a_write_request_whose_content_is_absent_or_not_base64_is_refused() -> None:
    with pytest.raises(InvalidAgentRequestError, match="content_b64 must be a string"):
        parse_write_file_request({"path": "a"}, _ROOT)
    with pytest.raises(InvalidAgentRequestError, match="content_b64 is not valid base64"):
        parse_write_file_request({"path": "a", "content_b64": "not base64!"}, _ROOT)
    with pytest.raises(InvalidAgentRequestError, match="content_b64 is not valid base64"):
        parse_write_file_request({"path": "a", "content_b64": "AAE"}, _ROOT)


def test_reading_answers_the_bytes_and_none_for_a_file_that_is_not_there(tmp_path: Path) -> None:
    (tmp_path / "note.bin").write_bytes(b"\x00binary\xff\n")
    assert read_workspace_file(tmp_path / "note.bin") == b"\x00binary\xff\n"
    assert read_workspace_file(tmp_path / "absent.txt") is None
    assert read_workspace_file(tmp_path / "absent-dir" / "absent.txt") is None


def test_reading_refuses_what_is_not_a_readable_regular_file(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("x")
    with pytest.raises(WorkspaceFileError, match="not a regular file"):
        read_workspace_file(tmp_path)
    with pytest.raises(WorkspaceFileError, match="Not a directory"):
        read_workspace_file(tmp_path / "note.txt" / "below")
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(WorkspaceFileError, match="not a regular file"):
        read_workspace_file(tmp_path / "pipe")


def test_reading_refuses_a_file_larger_than_a_write_could_carry(tmp_path: Path) -> None:
    at_the_limit = tmp_path / "at-the-limit"
    with at_the_limit.open("wb") as file:
        file.truncate(MAX_READ_FILE_BYTES)
    content = read_workspace_file(at_the_limit)
    assert content is not None and len(content) == MAX_READ_FILE_BYTES
    with at_the_limit.open("ab") as file:
        file.write(b"x")
    with pytest.raises(WorkspaceFileError, match=f"larger than {MAX_READ_FILE_BYTES} bytes"):
        read_workspace_file(at_the_limit)


def test_writing_creates_the_directory_sets_the_mode_and_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "made" / "on" / "the-way" / "note.txt"
    write_workspace_file(target, b"first\n", 0o640)
    assert target.read_bytes() == b"first\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    write_workspace_file(target, b"second\n", 0o600)
    assert target.read_bytes() == b"second\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [entry.name for entry in target.parent.iterdir()] == ["note.txt"]


def test_writing_over_a_directory_fails_and_leaves_no_temp_file(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    with pytest.raises(WorkspaceFileError, match="Is a directory"):
        write_workspace_file(tmp_path / "taken", b"x", 0o600)
    assert [entry.name for entry in tmp_path.iterdir()] == ["taken"]
    (tmp_path / "a-file").write_text("x")
    with pytest.raises(WorkspaceFileError, match="File exists|Not a directory"):
        write_workspace_file(tmp_path / "a-file" / "below", b"x", 0o600)
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["a-file", "taken"]


def test_a_line_is_one_event_whatever_reads_it_arrived_in() -> None:
    splitter = _LineSplitter("stdout")
    assert splitter.feed(b"one\ntwo") == [{"type": "stdout", "data": "one\n"}]
    assert splitter.feed(b" more\n\nthree") == [
        {"type": "stdout", "data": "two more\n"},
        {"type": "stdout", "data": "\n"},
    ]
    assert splitter.feed(b"") == [{"type": "stdout", "data": "three"}]
    assert splitter.feed(b"") == []


def test_a_character_split_across_two_reads_is_decoded_whole_and_bad_bytes_are_replaced() -> None:
    splitter = _LineSplitter("stderr")
    encoded = "café\n".encode()
    assert splitter.feed(encoded[:4]) == []
    assert splitter.feed(encoded[4:]) == [{"type": "stderr", "data": "café\n"}]
    assert splitter.feed(b"bad \xff byte\n") == [{"type": "stderr", "data": "bad � byte\n"}]


def test_a_line_that_never_ends_is_cut_into_bounded_events() -> None:
    splitter = _LineSplitter("stdout")
    events = splitter.feed(b"x" * (2 * 1024 * 1024 + 5))
    assert [len(str(event["data"])) for event in events] == [1024 * 1024, 1024 * 1024]
    assert splitter.feed(b"") == [{"type": "stdout", "data": "xxxxx"}]


def test_a_command_streams_each_line_of_both_outputs_and_then_its_exit_code(tmp_path: Path) -> None:
    events = run_to_completion(["sh", "-c", "echo one; echo two; echo trouble >&2; exit 3"], tmp_path)
    assert [event for event in events if event["type"] == "stdout"] == [
        {"type": "stdout", "data": "one\n"},
        {"type": "stdout", "data": "two\n"},
    ]
    assert [event for event in events if event["type"] == "stderr"] == [{"type": "stderr", "data": "trouble\n"}]
    assert events[-1] == {"type": "exit", "code": 3}
    assert len(events) == 4


def test_a_command_runs_directly_in_its_directory_with_no_shell_and_no_input(tmp_path: Path) -> None:
    (tmp_path / "here").mkdir()
    assert run_to_completion(["pwd"], tmp_path / "here") == [
        {"type": "stdout", "data": f"{(tmp_path / 'here').resolve()}\n"},
        {"type": "exit", "code": 0},
    ]
    # A shell would expand the variable and split the words; run directly, each word arrives as written.
    assert run_to_completion(["printf", "%s|", "$HOME", "a b", "*"], tmp_path) == [
        {"type": "stdout", "data": "$HOME|a b|*|"},
        {"type": "exit", "code": 0},
    ]
    assert run_to_completion(["cat"], tmp_path) == [{"type": "exit", "code": 0}]


def test_a_command_that_cannot_start_is_one_exit_event_saying_why(tmp_path: Path) -> None:
    name = "no-such-command-f3a9"
    events = run_to_completion([name], tmp_path)
    assert len(events) == 1
    assert events[0]["type"] == "exit" and events[0]["code"] is None and name in str(events[0]["error"])
    missing_directory = run_to_completion(["true"], tmp_path / "absent")
    assert len(missing_directory) == 1 and f"'{tmp_path / 'absent'}'" in str(missing_directory[0]["error"])
    assert missing_directory[0]["code"] is None
    null_byte = run_to_completion(["echo", "a\x00b"], tmp_path)
    assert len(null_byte) == 1 and null_byte[0]["code"] is None and "null" in str(null_byte[0]["error"])


def test_a_command_ended_by_a_signal_reports_owner_execs_code_for_it(tmp_path: Path) -> None:
    assert run_to_completion(["sh", "-c", "kill -TERM $$"], tmp_path) == [{"type": "exit", "code": -1}]


def test_a_command_out_of_time_is_killed_with_everything_it_started_and_says_so(tmp_path: Path) -> None:
    lifeline = ProcessLifeline(tmp_path)
    try:
        events = start_holding(lifeline, tmp_path, timeout_seconds=0.3, running=RunningCommands())
        assert list(events) == [{"type": "exit", "code": None, "timed_out": True}]
        assert lifeline.is_released_within(_LIFELINE_WAIT_SECONDS)
    finally:
        lifeline.close()


def test_a_command_whose_reader_stops_reading_is_killed_with_everything_it_started(tmp_path: Path) -> None:
    lifeline = ProcessLifeline(tmp_path)
    running = RunningCommands()
    try:
        events = start_holding(lifeline, tmp_path, timeout_seconds=600.0, running=running)
        assert not lifeline.is_released_within(0.0)
        events.close()
        assert lifeline.is_released_within(_LIFELINE_WAIT_SECONDS)
    finally:
        lifeline.close()
        running.kill_all()


def test_a_command_that_prints_nothing_is_killed_when_its_callers_connection_ends(tmp_path: Path) -> None:
    lifeline = ProcessLifeline(tmp_path)
    running = RunningCommands()
    ours, theirs = socket.socketpair()
    try:
        events = start_holding(lifeline, tmp_path, timeout_seconds=600.0, running=running, caller=ours)
        theirs.close()
        # Nobody is left to read an exit event, and the command had ten minutes left when it was ended.
        assert list(events) == []
        assert lifeline.is_released_within(_LIFELINE_WAIT_SECONDS)
    finally:
        ours.close()
        lifeline.close()
        running.kill_all()


def test_a_command_that_closed_its_output_is_killed_when_its_callers_connection_ends(tmp_path: Path) -> None:
    lifeline = ProcessLifeline(tmp_path)
    output_closed = ProcessLifeline(tmp_path)
    running = RunningCommands()
    ours, theirs = socket.socketpair()
    # What `sh -c 'cmd > log 2>&1'` comes to once its shell is cmd: both outputs dropped, and the command runs on.
    # It lets go of the second FIFO once they are.
    script = (
        f"exec 3>{lifeline.path} 4>{output_closed.path}; echo started; exec >/dev/null 2>&1; exec 4>&-; exec sleep 600"
    )
    events = stream_command(RunRequest(["sh", "-c", script], tmp_path, 600.0), running, ours)
    later_events: list[dict[str, object]] = []
    # The stream is read on a thread: a command with no output left yields nothing while it runs on.
    reader = threading.Thread(target=lambda: later_events.extend(events), daemon=True)
    try:
        assert next(events) == {"type": "stdout", "data": "started\n"}
        reader.start()
        assert output_closed.is_released_within(_LIFELINE_WAIT_SECONDS)
        theirs.close()
        reader.join(_LIFELINE_WAIT_SECONDS)
        assert not reader.is_alive()
        assert later_events == []
        assert lifeline.is_released_within(_LIFELINE_WAIT_SECONDS)
    finally:
        running.kill_all()
        if reader.is_alive():
            reader.join(_LIFELINE_WAIT_SECONDS)
        ours.close()
        lifeline.close()
        output_closed.close()


def test_bytes_a_caller_sends_while_its_command_runs_do_not_end_the_command(tmp_path: Path) -> None:
    go_on = tmp_path / "go-on"
    script = f"echo first; while [ ! -e {go_on} ]; do sleep 0.01; done; echo second"
    ours, theirs = socket.socketpair()
    try:
        events = stream_command(RunRequest(["sh", "-c", script], tmp_path, 5.0), RunningCommands(), ours)
        assert next(events) == {"type": "stdout", "data": "first\n"}
        theirs.sendall(b"POST /_agent/run HTTP/1.1\r\n")
        go_on.touch()
        assert list(events) == [{"type": "stdout", "data": "second\n"}, {"type": "exit", "code": 0}]
        # The bytes were looked at, not taken: they are still the next request's to read.
        assert ours.recv(64) == b"POST /_agent/run HTTP/1.1\r\n"
    finally:
        ours.close()
        theirs.close()


def test_a_gateway_that_stops_ends_the_commands_in_flight(tmp_path: Path) -> None:
    lifeline = ProcessLifeline(tmp_path)
    running = RunningCommands()
    try:
        events = start_holding(lifeline, tmp_path, timeout_seconds=600.0, running=running)
        running.kill_all()
        assert list(events) == [{"type": "exit", "code": -1}]
        assert lifeline.is_released_within(_LIFELINE_WAIT_SECONDS)
        # Nothing is left to end, so a second stop is a no-op rather than a signal to a process id since reused.
        running.kill_all()
    finally:
        lifeline.close()


def test_a_finished_command_does_not_take_what_it_left_running_with_it(tmp_path: Path) -> None:
    lifeline = ProcessLifeline(tmp_path)
    pid_file = tmp_path / "left-running.pid"
    # The child drops the command's output, so the command is finished when its shell exits.
    script = f"exec 3>{lifeline.path}; sleep 600 >/dev/null 2>&1 & echo $! > {pid_file}"
    try:
        assert run_to_completion(["sh", "-c", script], tmp_path) == [{"type": "exit", "code": 0}]
        assert not lifeline.is_released_within(0.0)
    finally:
        os.kill(int(pid_file.read_text()), signal.SIGKILL)
        assert lifeline.is_released_within(_LIFELINE_WAIT_SECONDS)
        lifeline.close()


@pytest.mark.skipif(sys.platform != "linux", reason="reads the process table through /proc")
def test_a_finished_command_is_reaped(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    assert run_to_completion(["sh", "-c", f"echo $$ > {pid_file}"], tmp_path) == [{"type": "exit", "code": 0}]
    assert not Path(f"/proc/{pid_file.read_text().strip()}").exists()
