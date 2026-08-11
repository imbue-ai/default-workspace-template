from imbue.system_interface.harnesses.sending_registry import SendingRegistry


def test_empty_registry_has_no_block() -> None:
    registry = SendingRegistry.build()
    assert registry.in_flight_texts() == []
    assert registry.concatenated_block() == ""


def test_records_are_returned_in_send_order() -> None:
    registry = SendingRegistry.build()
    registry.record("t1", "first")
    registry.record("t2", "second")
    assert registry.in_flight_texts() == ["first", "second"]
    assert registry.concatenated_block() == "first\nsecond"


def test_resolve_removes_only_the_named_token() -> None:
    registry = SendingRegistry.build()
    registry.record("t1", "first")
    registry.record("t2", "second")
    registry.resolve("t1")
    assert registry.in_flight_texts() == ["second"]


def test_resolve_of_unknown_token_is_a_noop() -> None:
    registry = SendingRegistry.build()
    registry.record("t1", "first")
    registry.resolve("nope")
    assert registry.in_flight_texts() == ["first"]


def test_duplicate_content_is_kept_distinct_by_token() -> None:
    registry = SendingRegistry.build()
    registry.record("t1", "same text")
    registry.record("t2", "same text")
    assert registry.in_flight_texts() == ["same text", "same text"]
    registry.resolve("t1")
    assert registry.in_flight_texts() == ["same text"]


def test_re_recording_a_token_replaces_in_place_without_duplicating() -> None:
    registry = SendingRegistry.build()
    registry.record("t1", "original")
    registry.record("t1", "updated")
    assert registry.in_flight_texts() == ["updated"]


def test_clear_drops_everything() -> None:
    registry = SendingRegistry.build()
    registry.record("t1", "first")
    registry.record("t2", "second")
    registry.clear()
    assert registry.in_flight_texts() == []
