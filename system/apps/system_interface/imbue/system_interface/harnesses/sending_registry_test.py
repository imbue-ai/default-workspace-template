from imbue.system_interface.harnesses.sending_registry import SendingRegistry
from imbue.system_interface.harnesses.sending_registry import SendingStateWatcherMixin


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


class _MixinHost(SendingStateWatcherMixin):
    """A minimal watcher-shaped host: constructed via ``__new__``/``build`` like the real
    watchers (no ``__init__``), calling the mixin's initializer from ``build``."""

    @classmethod
    def build(cls) -> "_MixinHost":
        self = cls.__new__(cls)
        self._init_sending_state()
        return self


def test_mixin_tracks_and_resolves_sending_state() -> None:
    host = _MixinHost.build()
    # note_sent_message records and returns a token; the block reflects send order.
    t1 = host.note_sent_message("first")
    host.note_sent_message("second", message_id="explicit-id")
    assert t1 is not None
    assert host.get_in_flight_block() == "first\nsecond"
    # commit / retract each drop exactly the named message.
    host.commit_sent_message(t1)
    assert host.get_in_flight_block() == "second"
    host.retract_sent_message("explicit-id")
    assert host.get_in_flight_block() == ""


def test_mixin_uses_the_supplied_message_id_as_the_token() -> None:
    host = _MixinHost.build()
    returned = host.note_sent_message("hi", message_id="stable-42")
    assert returned == "stable-42"


def test_mixin_has_its_own_lock_independent_of_any_subclass_lock() -> None:
    # The mixin's lock is private to the Sending concern, not a shared ``_lock`` a
    # subclass might also use for its transcript mirror.
    host = _MixinHost.build()
    assert not hasattr(host, "_lock")
    assert host._sending_lock is not None
