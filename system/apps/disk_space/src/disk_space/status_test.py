"""The one answer another agent asks for (``GET /api/status`` and ``disk-space status``), and the command line.

``build_status`` is the Caretaker's integration point, so these tests pin what it promises: the figures, the totals,
and the largest items of each verdict, in the order an agent would want to act on them.
"""

import json
import time
from pathlib import Path

import pytest

from disk_space import cli, helper, scan, status, store
from disk_space.testing import GIB, a_disk, a_node, install_disk, save_measurement


def test_the_status_before_any_measurement_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_disk(monkeypatch, a_disk(used=50 * GIB, free=50 * GIB, percent=50))
    answer = status.build_status()
    assert answer["percent_used"] == 50
    assert answer["measured_at"] is None
    assert answer["measured_minutes_ago"] is None
    assert answer["safe_to_delete"] == []
    assert answer["ask_first"] == []
    assert answer["safe_to_delete_bytes"] == 0
    assert answer["is_over_threshold"] is False
    assert "POST /api/delete" in answer["how_to_free_space"]


def test_the_status_totals_each_verdict_and_lists_the_biggest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_measurement(
        [
            a_node("root", None, "Whole disk"),
            a_node(
                "n1",
                "root",
                "Small cache",
                size=1 * GIB,
                path="/var/cache/user/uv",
                verdict=scan.SAFE,
                item=True,
                method="uv-cache",
            ),
            a_node(
                "n2",
                "root",
                "Large cache",
                size=7 * GIB,
                path="/var/cache/user/huggingface",
                verdict=scan.SAFE,
                item=True,
                method="remove",
            ),
            a_node(
                "n3",
                "root",
                "Your folder: notes",
                size=3 * GIB,
                path="/home/user/workspace/data/notes",
                verdict=scan.ASK,
                item=True,
                method="remove",
            ),
            a_node(
                "n4",
                "root",
                "Workspace code",
                size=20 * GIB,
                path="/home/user/workspace/system",
                verdict=scan.KEEP,
                item=True,
            ),
            a_node("n5", "root", "Your data", size=40 * GIB, verdict=scan.ASK),
        ],
        taken_at=time.time() - 5 * 60,
    )
    install_disk(monkeypatch, a_disk(used=93 * GIB, free=7 * GIB, percent=93))
    answer = status.build_status()
    assert answer["safe_to_delete_bytes"] == 8 * GIB
    assert answer["ask_first_bytes"] == 3 * GIB
    assert [item["name"] for item in answer["safe_to_delete"]] == [
        "Large cache",
        "Small cache",
    ]
    assert [item["name"] for item in answer["ask_first"]] == ["Your folder: notes"]
    assert answer["safe_to_delete"][0] == {
        "name": "Large cache",
        "path": "/var/cache/user/huggingface",
        "bytes": 7 * GIB,
        "why": "",
        "deletable": True,
    }
    # A folder that is only named, or only kept, is not something an agent can act on.
    assert all(
        item["name"] not in {"Your data", "Workspace code"}
        for item in answer["safe_to_delete"]
    )
    assert answer["measured_minutes_ago"] == 5
    assert answer["is_over_threshold"] is True
    assert answer["helper_chat_id"] is None


def test_the_status_lists_at_most_ten_of_each(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = [a_node("root", None, "Whole disk")]
    for index in range(14):
        nodes.append(
            a_node(
                f"n{index}",
                "root",
                f"Cache {index}",
                size=(index + 1) * GIB,
                path=f"/var/cache/c{index}",
                verdict=scan.SAFE,
                item=True,
                method="remove",
            )
        )
    save_measurement(nodes)
    install_disk(monkeypatch, a_disk())
    answer = status.build_status()
    assert len(answer["safe_to_delete"]) == status.ITEMS_LISTED
    assert answer["safe_to_delete_bytes"] == sum(range(1, 15)) * GIB
    assert answer["safe_to_delete"][0]["name"] == "Cache 13"


def test_the_status_reports_the_helper_chat_and_the_last_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.update_watch(helper_chat_id="chat-abc", last_check_at=1234.0)
    install_disk(monkeypatch, a_disk())
    answer = status.build_status()
    assert answer["helper_chat_id"] == "chat-abc"
    assert answer["last_check_at"] == 1234.0


def test_the_status_of_an_unshared_workspace_names_its_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.write_settings(95)
    install_disk(monkeypatch, a_disk(percent=93))
    answer = status.build_status()
    assert answer["threshold_percent"] == 95
    assert answer["is_over_threshold"] is False
    assert answer["percent_used"] == 93


def test_a_measurement_taken_at_the_line_counts_as_over_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_disk(monkeypatch, a_disk(percent=90))
    assert status.build_status()["is_over_threshold"] is True


def test_the_command_prints_the_answer_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_disk(monkeypatch, a_disk(percent=93))
    assert cli.main(["status", "--json"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["percent_used"] == 93
    assert "how_to_free_space" in answer


def test_the_command_reads_in_plain_words(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    save_measurement(
        [
            a_node("root", None, "Whole disk"),
            a_node(
                "n1",
                "root",
                "AI model downloads",
                size=7 * GIB,
                path="/var/cache/user/huggingface",
                verdict=scan.SAFE,
                item=True,
                method="remove",
            ),
        ]
    )
    install_disk(monkeypatch, a_disk(percent=93))
    assert cli.main(["status"]) == 0
    printed = capsys.readouterr().out
    assert "93% full, 7.0 GB free (alert line 90%)." in printed
    assert "Safe to delete: 7.0 GB; ask first: 0.0 GB." in printed
    assert (
        "safe    7.0 GB  AI model downloads  (/var/cache/user/huggingface)" in printed
    )


def test_the_command_deletes_what_the_measurement_allows_and_says_what_it_freed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cache = tmp_path / "leftover"
    cache.mkdir()
    (cache / "blob").write_bytes(b"x" * 10)
    save_measurement(
        [
            a_node("root", None, "Whole disk"),
            a_node(
                "n1",
                "root",
                "Scratch: leftover",
                size=10,
                path=str(cache),
                verdict=scan.SAFE,
                item=True,
                method="remove",
            ),
        ]
    )
    install_disk(monkeypatch, a_disk(free=7 * GIB), a_disk(free=8 * GIB))
    assert cli.main(["delete", str(cache)]) == 0
    printed = capsys.readouterr().out
    assert f"deleted {cache} (0.0 GB)" in printed
    assert "Freed 1.0 GB." in printed
    assert not cache.exists()


def test_the_command_refuses_what_it_may_not_delete_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()
    save_measurement([a_node("root", None, "Whole disk")])
    install_disk(monkeypatch, a_disk())
    assert cli.main(["delete", str(folder), "--user-approved"]) == 1
    printed = capsys.readouterr()
    assert (
        f"refused {folder}: It is not one of the items the last measurement listed."
        in printed.err
    )
    assert folder.exists()


def test_the_command_measures_and_says_what_it_took(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    save_measurement([a_node("root", None, "Whole disk", size=1)])
    monkeypatch.setattr(
        scan,
        "scan_locked",
        lambda: {"id": "20240101T000000-abcdef", "took_seconds": 12.0},
    )
    assert cli.main(["scan"]) == 0
    assert (
        "Measured in 12.0s; snapshot 20240101T000000-abcdef." in capsys.readouterr().out
    )


def test_the_command_says_when_a_measurement_is_already_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(scan, "scan_locked", lambda: None)
    assert cli.main(["scan"]) == 0
    assert "A measurement is already running." in capsys.readouterr().out


def test_the_command_starts_the_helper_and_prints_its_chat(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        helper,
        "start",
        lambda reason, trigger, item=None: {
            "chat_id": "chat-abc",
            "created": True,
            "detail": "",
        },
    )
    assert cli.main(["helper", "--item", "/var/tmp/x"]) == 0
    assert json.loads(capsys.readouterr().out)["chat_id"] == "chat-abc"


def test_the_command_runs_the_check_and_prints_its_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli.check,
        "run_check",
        lambda: {
            "summary": "The disk is 93% full. Started the cleanup helper.",
            "alerted": True,
            "chat_id": "chat-abc",
        },
    )
    assert cli.main(["check"]) == 0
    assert (
        "The disk is 93% full. Started the cleanup helper." in capsys.readouterr().out
    )
