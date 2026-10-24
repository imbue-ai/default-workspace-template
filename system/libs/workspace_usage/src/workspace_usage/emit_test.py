import json
import threading
from pathlib import Path

from workspace_usage.emit import event_file_name, record_model_call


def _event(origin_kind: str, origin_id: str | None, event_id: str) -> dict[str, object]:
    attribution = {"workspace_id": "ws-1", "origin_kind": origin_kind}
    if origin_id is not None:
        attribution["origin_id"] = origin_id
    return {"event_id": event_id, "attribution": attribution}


def test_an_event_is_appended_to_its_origins_file(tmp_path: Path) -> None:
    environ = {"IMBUE_USAGE_DATA_DIR": str(tmp_path)}

    first = record_model_call(_event("APP", "files", "e-1"), environ)
    second = record_model_call(_event("APP", "files", "e-2"), environ)

    assert first is not None
    assert first == second == tmp_path / "events" / "app-files.jsonl"
    assert [
        json.loads(line)["event_id"] for line in first.read_text().splitlines()
    ] == ["e-1", "e-2"]


def test_file_names_are_safe_and_unattributed_events_share_one_file() -> None:
    assert (
        event_file_name(_event("CHAT", "../../etc/passwd", "e"))
        == "chat-.._.._etc_passwd.jsonl"
    )
    assert event_file_name(_event("UNKNOWN", None, "e")) == "unknown.jsonl"
    assert event_file_name({"event_id": "e"}) == "unknown.jsonl"


def test_concurrent_writers_never_interleave_within_a_line(tmp_path: Path) -> None:
    environ = {"IMBUE_USAGE_DATA_DIR": str(tmp_path)}
    padding = "x" * 2000

    def write_many(writer: int) -> None:
        for index in range(50):
            record_model_call(
                {**_event("APP", "files", f"{writer}-{index}"), "padding": padding},
                environ,
            )

    threads = [
        threading.Thread(target=write_many, args=(writer,)) for writer in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = (tmp_path / "events" / "app-files.jsonl").read_text().splitlines()
    assert len({json.loads(line)["event_id"] for line in lines}) == 400


def test_an_unwritable_data_dir_is_logged_not_raised(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the directory should be")

    assert (
        record_model_call(
            _event("APP", "files", "e-3"), {"IMBUE_USAGE_DATA_DIR": str(blocker)}
        )
        is None
    )
