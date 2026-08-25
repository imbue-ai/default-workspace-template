from imbue.remote_service_connector.suspension import get_suspended_at
from imbue.remote_service_connector.testing import _seed_entitlements_row
from imbue.remote_service_connector.testing import make_fake_entitlements_store

_USER_ID = "11111111-2222-3333-4444-555566667777"


def test_get_suspended_at_returns_none_without_an_entitlements_row() -> None:
    store = make_fake_entitlements_store()

    assert get_suspended_at("unknown-user-id", store=store) is None


def test_get_suspended_at_returns_none_for_an_unsuspended_row() -> None:
    store = make_fake_entitlements_store()
    _seed_entitlements_row(store, user_id=_USER_ID, user_id_prefix="1111111122223333")

    assert get_suspended_at(_USER_ID, store=store) is None


def test_get_suspended_at_reflects_the_flag_set_and_cleared() -> None:
    store = make_fake_entitlements_store()
    _seed_entitlements_row(store, user_id=_USER_ID, user_id_prefix="1111111122223333")

    store.update_entitlements(_USER_ID, {"suspended_at": "2026-08-22T00:00:00+00:00", "suspended_reason": "abuse"})
    assert get_suspended_at(_USER_ID, store=store) == "2026-08-22T00:00:00+00:00"

    store.update_entitlements(_USER_ID, {"suspended_at": None, "suspended_reason": None})
    assert get_suspended_at(_USER_ID, store=store) is None
