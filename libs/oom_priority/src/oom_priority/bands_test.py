"""Invariants for the memory-shedding priority bands.

These guard the graceful-degradation guarantee against an accidental edit to the
band values: services must stay below agents, user-created services must sit
above every built-in service but below the agent bands, and the built-in
services must keep the documented least- to most-expendable order.
"""

from oom_priority import bands

# The built-in services in their documented order, least- to most-expendable.
# User-created services (the "user" key) are excluded -- they are asserted
# separately as sitting above every one of these.
_BUILTIN_SERVICE_ORDER = (
    "terminal",
    "system_interface",
    "cloudflared",
    "runtime-backup",
    "host-backup",
    "app-watcher",
    "web",
)


def test_builtin_services_are_strictly_ordered_least_to_most_expendable() -> None:
    values = [bands.SERVICE_BANDS[key] for key in _BUILTIN_SERVICE_ORDER]
    assert values == sorted(values), values
    assert len(set(values)) == len(values), "built-in service bands must be distinct"


def test_every_service_band_sits_between_protected_and_the_user_agent() -> None:
    # A service is less expendable than any agent (agents revive on the next
    # message, so they are shed first) but more expendable than the never-kill
    # infrastructure at PROTECTED.
    for key, adj in bands.SERVICE_BANDS.items():
        assert bands.PROTECTED < adj < bands.USER_AGENT, (key, adj)


def test_user_created_services_are_shed_before_every_builtin_service() -> None:
    user_band = bands.SERVICE_BANDS["user"]
    assert user_band == bands.USER_SERVICE
    for key in _BUILTIN_SERVICE_ORDER:
        assert bands.SERVICE_BANDS[key] < user_band, key


def test_the_builtin_key_set_matches_the_documented_order() -> None:
    # Catch a service added to SERVICE_BANDS without being placed in the ordering
    # above (which would leave its rank unasserted).
    assert set(bands.SERVICE_BANDS) == {*_BUILTIN_SERVICE_ORDER, "user"}
