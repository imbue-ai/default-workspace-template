from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app_instances.data_types import (
    CreateRequest,
    InstanceLifetime,
    InstanceRecord,
    InstanceStatus,
    LocationRequest,
    RenameRequest,
)
from app_instances.primitives import InstanceKey, InstanceTitle, InstanceUrl


def _record(last_active: datetime | None) -> InstanceRecord:
    return InstanceRecord(
        key=InstanceKey("terminal-2"),
        url=InstanceUrl("/?arg=session&arg=terminal-2&arg={tab}"),
        title=InstanceTitle("Terminal 2"),
        status=InstanceStatus.IDLE,
        lifetime=InstanceLifetime.EXPLICIT,
        last_active=last_active,
        renameable=True,
    )


def test_record_json_is_the_contract_shape_with_last_active_in_utc() -> None:
    two_hours_east = timezone(timedelta(hours=2))
    record = _record(datetime(2026, 9, 2, 16, 11, 2, 824000, tzinfo=two_hours_east))

    assert record.model_dump(mode="json") == {
        "key": "terminal-2",
        "url": "/?arg=session&arg=terminal-2&arg={tab}",
        "title": "Terminal 2",
        "status": "idle",
        "lifetime": "explicit",
        "last_active": "2026-09-02T14:11:02.824000Z",
        "renameable": True,
    }


def test_record_json_keeps_an_unknown_last_active_as_null() -> None:
    assert _record(None).model_dump(mode="json")["last_active"] is None


def test_record_rejects_a_naive_last_active() -> None:
    with pytest.raises(ValidationError, match="last_active"):
        _record(datetime(2026, 9, 2, 14, 11, 2))


def test_record_parses_the_wire_timestamp_back_to_an_aware_datetime() -> None:
    wire = _record(None).model_dump(mode="json") | {
        "last_active": "2026-09-02T14:11:02.824Z"
    }

    parsed = InstanceRecord.model_validate(wire)

    assert parsed.last_active == datetime(
        2026, 9, 2, 14, 11, 2, 824000, tzinfo=timezone.utc
    )


def test_record_rejects_a_status_outside_the_contract() -> None:
    with pytest.raises(ValidationError, match="status"):
        InstanceRecord.model_validate(
            _record(None).model_dump(mode="json") | {"status": "busy"}
        )


def test_create_request_defaults_params_to_empty_and_rejects_non_string_values() -> (
    None
):
    assert CreateRequest.model_validate({"action": "new"}).params == {}
    assert CreateRequest.model_validate(
        {"action": "new", "params": {"path": "/x"}}
    ).params == {"path": "/x"}
    with pytest.raises(ValidationError, match="params"):
        CreateRequest.model_validate({"action": "new", "params": {"path": 3}})
    with pytest.raises(ValidationError, match="action"):
        CreateRequest.model_validate({"action": "Not An Id"})


def test_rename_and_location_requests_apply_the_primitive_rules() -> None:
    assert RenameRequest.model_validate({"title": "  Two  "}).title == "Two"
    assert LocationRequest.model_validate({"path": "/docs/"}).path == "/docs/"
    with pytest.raises(ValidationError, match="title"):
        RenameRequest.model_validate({"title": " "})
    with pytest.raises(ValidationError, match="path"):
        LocationRequest.model_validate({"path": "docs"})
