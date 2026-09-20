"""Tests for the secret-request store: the env file it writes is exactly what the wrapper
reads back, the request lifecycle, and the values' absence from everything observable."""

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from imbue.chat.secret_requests import InvalidSecretRequestError
from imbue.chat.secret_requests import SecretRequestNotPendingError
from imbue.chat.secret_requests import SecretRequestStatus
from imbue.chat.secret_requests import SecretRequestStore
from imbue.chat.secret_requests import SecretValuesMismatchError
from imbue.chat.secret_requests import UnknownSecretRequestError
from imbue.chat.secret_requests import env_variable_names
from imbue.chat.secret_requests import merge_env_text

# The wrapper the written file is read back through: the one reader that matters.
_WITH_SECRETS = Path(__file__).resolve().parents[4] / "scripts" / "with_secrets.py"

# Every shell-significant character at once, plus a newline and trailing whitespace.
_AWKWARD_VALUE = "it's $HOME `x` \\ a=b#c\nline two  "
_CHAT = "agent-00000000000000000000000000000001"


def _store(tmp_path: Path) -> SecretRequestStore:
    workspace = tmp_path / "workspace"
    return SecretRequestStore(
        requests_directory=workspace / "data" / ".apps" / "chat" / "secret-requests",
        secrets_directory=workspace / "data" / ".secrets",
    )


def _read_back_through_the_wrapper(env_file: Path, *names: str) -> dict[str, str | None]:
    reporter = "import json, os, sys; print(json.dumps({n: os.environ.get(n) for n in sys.argv[1:]}))"
    completed = subprocess.run(
        [sys.executable, str(_WITH_SECRETS), str(env_file), "--", sys.executable, "-c", reporter, *names],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_a_submitted_value_round_trips_byte_for_byte_through_the_wrapper(tmp_path: Path) -> None:
    store = _store(tmp_path)
    filed = store.file_request(_CHAT, "svc", ["SVC_TOKEN", "SVC_URL"], "to call the widget API")
    stored = store.submit(filed.request.request_id, {"SVC_TOKEN": _AWKWARD_VALUE, "SVC_URL": "https://x.example/"})

    env_file = tmp_path / "workspace" / stored.env_path
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert _read_back_through_the_wrapper(env_file, "SVC_TOKEN", "SVC_URL") == {
        "SVC_TOKEN": _AWKWARD_VALUE,
        "SVC_URL": "https://x.example/",
    }
    assert stored.status is SecretRequestStatus.STORED


def test_a_submit_merges_into_an_existing_file_keeping_unrelated_entries_and_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.file_request(_CHAT, "svc", ["A", "B"], "first")
    store.submit(first.request.request_id, {"A": "1", "B": "2"})
    # A hand-written comment survives too; the merge keeps everything it does not replace.
    env_file = tmp_path / "workspace" / first.request.env_path
    env_file.write_text("# keep me\n" + env_file.read_text())

    second = store.file_request(_CHAT, "svc", ["B", "C"], "second")
    assert second.existing_variables == ("A", "B")
    assert second.overwrites == ("B",)
    store.submit(second.request.request_id, {"B": "two", "C": "3"})

    assert env_file.read_text() == "# keep me\nA='1'\nB='two'\nC='3'\n"
    assert _read_back_through_the_wrapper(env_file, "A", "B", "C") == {"A": "1", "B": "two", "C": "3"}


def test_a_multi_line_value_is_replaced_whole_on_a_later_merge() -> None:
    existing = "A='one\ntwo'\nB='x'\n"
    assert env_variable_names(existing) == ("A", "B")
    assert merge_env_text(existing, {"A": "flat"}) == "A='flat'\nB='x'\n"


def test_a_second_pending_request_for_the_same_file_supersedes_the_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.file_request(_CHAT, "svc", ["A"], "first")
    other_file = store.file_request(_CHAT, "other", ["A"], "another file")
    second = store.file_request("agent-00000000000000000000000000000002", "svc", ["A"], "second")

    assert [request.request_id for request in second.superseded] == [first.request.request_id]
    superseded = store.get(first.request.request_id)
    assert superseded is not None
    assert superseded.status is SecretRequestStatus.SUPERSEDED
    assert superseded.superseded_by == second.request.request_id
    # A pending request for another file is untouched.
    untouched = store.get(other_file.request.request_id)
    assert untouched is not None and untouched.status is SecretRequestStatus.PENDING
    with pytest.raises(SecretRequestNotPendingError):
        store.submit(first.request.request_id, {"A": "1"})


def test_a_decline_records_the_note_and_writes_no_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    filed = store.file_request(_CHAT, "svc", ["A"], "why")
    declined = store.decline(filed.request.request_id, "not now")
    assert declined.status is SecretRequestStatus.DECLINED
    assert declined.note == "not now"
    assert not (tmp_path / "workspace" / declined.env_path).exists()
    with pytest.raises(SecretRequestNotPendingError):
        store.decline(filed.request.request_id, None)


def test_requests_survive_a_new_store_over_the_same_directory(tmp_path: Path) -> None:
    filed = _store(tmp_path).file_request(_CHAT, "svc", ["A"], "why")
    reopened = _store(tmp_path).get(filed.request.request_id)
    assert reopened is not None
    assert reopened.status is SecretRequestStatus.PENDING
    assert reopened.variables == ("A",)


def test_a_submit_must_name_exactly_the_requested_variables(tmp_path: Path) -> None:
    store = _store(tmp_path)
    filed = store.file_request(_CHAT, "svc", ["A", "B"], "why")
    with pytest.raises(SecretValuesMismatchError):
        store.submit(filed.request.request_id, {"A": "1"})
    with pytest.raises(SecretValuesMismatchError):
        store.submit(filed.request.request_id, {"A": "1", "B": ""})
    with pytest.raises(UnknownSecretRequestError):
        store.submit("secret-00000000000000000000000000000000", {"A": "1"})
    assert store.get("not-an-id") is None


@pytest.mark.parametrize(
    ("file", "variables", "rationale"),
    [
        ("../etc", ["A"], "why"),
        ("Svc", ["A"], "why"),
        ("svc", [], "why"),
        ("svc", ["1A"], "why"),
        ("svc", ["A", "A"], "why"),
        ("svc", ["A"], "   "),
    ],
)
def test_malformed_filings_are_refused(tmp_path: Path, file: str, variables: list[str], rationale: str) -> None:
    with pytest.raises(InvalidSecretRequestError):
        _store(tmp_path).file_request(_CHAT, file, variables, rationale)


def test_the_value_appears_nowhere_but_the_env_file(tmp_path: Path, loguru_records: list[str]) -> None:
    store = _store(tmp_path)
    filed = store.file_request(_CHAT, "svc", ["A"], "why")
    value = "hunter2-" + "z" * 24
    store.submit(filed.request.request_id, {"A": value})
    record = (
        tmp_path / "workspace" / "data" / ".apps" / "chat" / "secret-requests" / f"{filed.request.request_id}.json"
    ).read_text()
    assert value not in record
    assert all(value not in line for line in loguru_records)
