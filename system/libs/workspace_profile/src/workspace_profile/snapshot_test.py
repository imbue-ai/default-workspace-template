import json
from pathlib import Path

from workspace_profile.snapshot import (
    SNAPSHOT_RELATIVE_PATH,
    SUPPORTED_SNAPSHOT_FORMAT,
    load_workspace_profile_snapshot,
    parse_workspace_profile_snapshot,
)


def _write_snapshot(workspace_root: Path, document: dict[str, object]) -> None:
    path = workspace_root / SNAPSHOT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_load_snapshot_reads_the_header_and_the_setting_values(tmp_path: Path) -> None:
    _write_snapshot(
        tmp_path,
        {
            "snapshot_format": 1,
            "account_id": "account-123",
            "owner_user_id": "user-123",
            "written_at": "2026-10-28T00:00:00+00:00",
            "written_by": "device-abc",
            "keybindings": {"shell": {"open": ["meta+k"]}},
        },
    )

    snapshot = load_workspace_profile_snapshot(tmp_path)

    assert snapshot is not None
    assert snapshot.account_id == "account-123"
    assert snapshot.owner_user_id == "user-123"
    assert snapshot.written_by == "device-abc"
    assert snapshot.value_by_key == {"keybindings": {"shell": {"open": ["meta+k"]}}}


def test_load_snapshot_returns_none_when_no_client_has_written_one(
    tmp_path: Path,
) -> None:
    assert load_workspace_profile_snapshot(tmp_path) is None


def test_load_snapshot_tolerates_an_unparseable_file(tmp_path: Path) -> None:
    path = tmp_path / SNAPSHOT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert load_workspace_profile_snapshot(tmp_path) is None


def test_parse_snapshot_rejects_a_document_that_is_not_an_object() -> None:
    assert parse_workspace_profile_snapshot(json.dumps(["not", "an", "object"])) is None


def test_parse_snapshot_keeps_resolving_from_a_newer_format() -> None:
    snapshot = parse_workspace_profile_snapshot(
        json.dumps(
            {
                "snapshot_format": SUPPORTED_SNAPSHOT_FORMAT + 1,
                "account_id": "account-123",
                "owner_user_id": "user-123",
                "a_newer_clients_setting": True,
            }
        )
    )

    assert snapshot is not None
    assert snapshot.snapshot_format == SUPPORTED_SNAPSHOT_FORMAT + 1
    assert snapshot.value_by_key == {"a_newer_clients_setting": True}


def test_parse_snapshot_defaults_a_missing_header_to_the_local_account() -> None:
    snapshot = parse_workspace_profile_snapshot("{}")

    assert snapshot is not None
    assert snapshot.account_id == "local"
    assert snapshot.owner_user_id == "local"


def test_parse_snapshot_rejects_a_header_field_of_the_wrong_type() -> None:
    assert (
        parse_workspace_profile_snapshot(json.dumps({"snapshot_format": "one"})) is None
    )
