"""Invariants for the memory-shedding priority bands.

These guard the graceful-degradation guarantee against an accidental edit to the
band values: services must stay below agents, user-created services must sit
above every built-in service but below the agent bands, and the built-in
services must keep the documented least- to most-expendable order.
"""

from oom_priority import bands

_HOUR = 3600.0


def _fresh(*, is_open: bool, is_visible: bool, recency_rank: int | None) -> int:
    """The score for a just-engaged chat: the engagement-only behaviour."""
    return bands.chat_agent_oom_score_adj(
        is_open=is_open,
        is_visible=is_visible,
        recency_rank=recency_rank,
        idle_seconds=0.0,
        is_mid_turn=False,
    )


def _aged(
    idle_seconds: float,
    *,
    is_open: bool = False,
    is_visible: bool = False,
    recency_rank: int | None = None,
    is_mid_turn: bool = False,
) -> int:
    """The score for a chat last engaged with ``idle_seconds`` ago."""
    return bands.chat_agent_oom_score_adj(
        is_open=is_open,
        is_visible=is_visible,
        recency_rank=recency_rank,
        idle_seconds=idle_seconds,
        is_mid_turn=is_mid_turn,
    )


# The built-in services in their documented order, least- to most-expendable.
# User-created services (the "user" key) are excluded -- they are asserted
# separately as sitting above every one of these.
_BUILTIN_SERVICE_ORDER = (
    "owner-exec",
    "terminal",
    "system_interface",
    "share-gateway",
    "github-sync",
    "host-backup",
    "cron",
    "app-watcher",
    "xvfb",
    "browser",
    "files",
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


def test_unrecognized_supervisord_program_falls_back_to_the_user_service_band() -> None:
    # The core fail-expendable guarantee: a program the policy does not know
    # (a user-created service that skipped the tagging prefix) must default to
    # the user-service band, never to a protected one.
    assert bands.supervisord_program_band("some-user-service") == bands.USER_SERVICE


def test_supervisord_program_bands_preserve_the_shedding_order() -> None:
    # Program names that double as service keys resolve to their service band;
    # the OOM machinery itself stays protected; the browser stays the single
    # most-expendable thing, above even an agent's subprocesses.
    for key in _BUILTIN_SERVICE_ORDER:
        assert bands.supervisord_program_band(key) == bands.SERVICE_BANDS[key]
    assert bands.supervisord_program_band("earlyoom") == bands.PROTECTED
    assert bands.supervisord_program_band("oom-tag-backstop") == bands.PROTECTED
    assert bands.SHARED_BROWSER > bands.AGENT_SUBPROCESS
    # The `browser` program is the coordinator, not Chromium: it resolves to its
    # service band, never to the shared-browser band its children occupy.
    assert bands.supervisord_program_band("browser") == bands.SERVICE_BANDS["browser"]


def test_primary_agent_is_pinned_to_the_never_shed_band() -> None:
    # The primary (services) agent must be at least as protected as the never-kill
    # infrastructure, and strictly below every service and agent band, so it is
    # shed dead last.
    assert bands.PRIMARY_AGENT == bands.PROTECTED
    assert bands.PRIMARY_AGENT < min(bands.SERVICE_BANDS.values())
    assert bands.PRIMARY_AGENT < bands.USER_AGENT


def test_chat_band_range_straddles_the_worker_band() -> None:
    # A fresh chat stays below WORKER_AGENT (workers are shed first) and above the
    # user-service band (a chat revives on its next message, so it is shed before a
    # service). A stale one crosses the worker band -- but never reaches
    # AGENT_SUBPROCESS, so an agent's own subprocesses are always shed before any
    # agent.
    assert bands.USER_SERVICE < bands.CHAT_AGENT_FLOOR
    assert bands.CHAT_AGENT_FLOOR < bands.CHAT_AGENT_BASE < bands.WORKER_AGENT
    assert bands.WORKER_AGENT < bands.CHAT_AGENT_STALE_CEILING < bands.AGENT_SUBPROCESS


def test_chat_score_is_most_protected_when_fully_engaged() -> None:
    engaged = _fresh(is_open=True, is_visible=True, recency_rank=0)
    idle = _fresh(is_open=False, is_visible=False, recency_rank=None)
    assert engaged == bands.CHAT_AGENT_FLOOR
    assert idle == bands.CHAT_AGENT_BASE
    # A fully engaged chat is the most protected; a closed, never-messaged one the least.
    assert engaged < idle


def test_never_messaged_chat_gets_no_recency_bonus() -> None:
    # ``None`` (never messaged) must not be treated as the most-recent (rank 0):
    # an open+visible chat that was never messaged is less protected than one that
    # was just messaged.
    never = _fresh(is_open=True, is_visible=True, recency_rank=None)
    just_messaged = _fresh(is_open=True, is_visible=True, recency_rank=0)
    assert just_messaged < never


def test_chat_score_monotonic_in_each_signal() -> None:
    base = _fresh(is_open=False, is_visible=False, recency_rank=5)
    opened = _fresh(is_open=True, is_visible=False, recency_rank=5)
    visible = _fresh(is_open=True, is_visible=True, recency_rank=5)
    more_recent = _fresh(is_open=False, is_visible=False, recency_rank=2)
    never = _fresh(is_open=False, is_visible=False, recency_rank=None)
    # Each engagement signal only ever lowers (more-protects) the score.
    assert opened < base
    assert visible < opened
    assert more_recent < base
    # Any messaged rank is at least as protected as never-messaged.
    assert base <= never


def test_browser_remap_lands_inside_the_band_and_preserves_chromes_order() -> None:
    # Chrome's self-assigned gradation (browser/zygote 0, gpu/utility 200,
    # renderers 300) must map to strictly increasing values that all sit inside
    # the browser band's range -- i.e. above every agent subprocess.
    remapped = [bands.shared_browser_oom_score_adj(v) for v in (0, 200, 300)]
    assert remapped == sorted(remapped)
    assert len(set(remapped)) == len(remapped), (
        "Chrome's gradation must survive the remap"
    )
    for value in remapped:
        assert bands.SHARED_BROWSER_FLOOR <= value <= bands.SHARED_BROWSER
    assert bands.AGENT_SUBPROCESS < bands.SHARED_BROWSER_FLOOR < bands.SHARED_BROWSER


def test_renderers_land_at_the_very_top_of_the_browser_band() -> None:
    # A renderer holds most of a browser's memory and costs a single tab to shed,
    # so it must be the most expendable process in the workspace -- not merely
    # somewhere inside the band. Scaling Chrome's gradation against 0..1000
    # rather than its real 0..300 range put renderers at 937, leaving the top of
    # the band to processes that had only *inherited* a high value and held
    # almost no memory.
    assert (
        bands.shared_browser_oom_score_adj(bands.CHROMIUM_SELF_ASSIGNED_MAX)
        == bands.SHARED_BROWSER
    )
    assert bands.shared_browser_oom_score_adj(0) == bands.SHARED_BROWSER_FLOOR


def test_browser_coordinator_is_shed_after_the_chromium_it_manages() -> None:
    # The coordinator is a service, not a browser: it holds little memory, the
    # Chromium processes outlive its death, and supervisord restarts it straight
    # back into the same session. Ranked inside the browser band it would be
    # picked first under pressure and free none of the memory that matters, so it
    # must sit below the whole band -- and, like any service, below the agents.
    coordinator = bands.SERVICE_BANDS["browser"]
    assert coordinator < bands.SHARED_BROWSER_FLOOR
    assert coordinator < bands.shared_browser_oom_score_adj(0)
    assert coordinator < bands.USER_AGENT


def test_browser_remap_output_is_never_below_the_floor() -> None:
    # The browser service's sweep only remaps values *below* the floor; the remap
    # emitting values at/above the floor is what makes repeated sweeps idempotent
    # (a remapped process is never remapped again). Out-of-range inputs clamp.
    for value in (-1000, -1, 0, 1, 299, 300, 999, 1000, 2000):
        remapped = bands.shared_browser_oom_score_adj(value)
        assert bands.SHARED_BROWSER_FLOOR <= remapped <= bands.SHARED_BROWSER, value


def test_chat_score_always_within_the_chat_band() -> None:
    for is_open in (True, False):
        for is_visible in (True, False):
            for rank in (0, 1, 3, 10, 100, None):
                for idle in (
                    None,
                    0.0,
                    1 * _HOUR,
                    6 * _HOUR,
                    24 * _HOUR,
                    3650 * 24 * _HOUR,
                ):
                    for mid_turn in (True, False):
                        adj = bands.chat_agent_oom_score_adj(
                            is_open=is_open,
                            is_visible=is_visible,
                            recency_rank=rank,
                            idle_seconds=idle,
                            is_mid_turn=mid_turn,
                        )
                        assert (
                            bands.CHAT_AGENT_FLOOR
                            <= adj
                            <= bands.CHAT_AGENT_STALE_CEILING
                        )
                        # However stale, a chat is never shed before an agent's own
                        # subprocesses, and never before a user-created service.
                        assert bands.USER_SERVICE < adj < bands.AGENT_SUBPROCESS


def test_an_abandoned_chat_is_shed_before_a_worker() -> None:
    # The point of the stale ceiling: a chat nobody has touched in a day is worth
    # less than the worker a live chat just spawned. It revives on its next message
    # with its transcript intact; the worker's in-flight work does not.
    assert _aged(24 * _HOUR) == bands.CHAT_AGENT_STALE_CEILING
    assert _aged(24 * _HOUR) > bands.WORKER_AGENT
    assert _aged(30 * 24 * _HOUR) == bands.CHAT_AGENT_STALE_CEILING


def test_staleness_crosses_the_worker_band_within_a_few_hours() -> None:
    # The documented schedule for a chat with no engagement at all: unchanged for
    # the first hour, still worth more than a worker at four hours, worth less by
    # six. The exact crossing is a tunable, but it must land inside a working day.
    assert _aged(0.5 * _HOUR) == bands.CHAT_AGENT_BASE
    assert _aged(1 * _HOUR) == bands.CHAT_AGENT_BASE
    assert _aged(4 * _HOUR) < bands.WORKER_AGENT
    assert _aged(6 * _HOUR) > bands.WORKER_AGENT


def test_engagement_delays_the_climb_but_never_prevents_it() -> None:
    # A visible, recently-ranked tab is still protected hours in -- but a tab left
    # open and untouched for a day is not protection, so it ends at the ceiling
    # like any other abandoned chat. Age wins over presence.
    def engaged(idle_seconds: float) -> int:
        return _aged(idle_seconds, is_open=True, is_visible=True, recency_rank=0)

    assert engaged(6 * _HOUR) < bands.WORKER_AGENT
    assert engaged(6 * _HOUR) > engaged(0.0)
    assert engaged(24 * _HOUR) == bands.CHAT_AGENT_STALE_CEILING


def test_chat_score_rises_monotonically_with_idle_time() -> None:
    # Whatever the engagement, more idle time is never *more* protective.
    for is_open, is_visible, rank in (
        (False, False, None),
        (True, False, None),
        (True, True, 0),
    ):
        scores = [
            _aged(idle, is_open=is_open, is_visible=is_visible, recency_rank=rank)
            for idle in (0.0, 1 * _HOUR, 4 * _HOUR, 12 * _HOUR, 24 * _HOUR)
        ]
        assert scores == sorted(scores), (is_open, is_visible, rank, scores)


def test_mid_turn_chat_is_never_demoted_past_a_worker() -> None:
    # A chat mid-turn is doing work that a shed would destroy outright (unlike an
    # idle chat, whose transcript survives and resumes), so age must not move it.
    assert _aged(30 * 24 * _HOUR, is_mid_turn=True) == bands.CHAT_AGENT_BASE
    assert _aged(30 * 24 * _HOUR, is_mid_turn=True) < bands.WORKER_AGENT
    assert _aged(
        30 * 24 * _HOUR, is_open=True, is_visible=True, recency_rank=0, is_mid_turn=True
    ) == (bands.CHAT_AGENT_FLOOR)


def test_unknown_idle_time_is_treated_as_fresh() -> None:
    # No engagement evidence at all (no reported activity, no process-start marker)
    # must not read as "abandoned": a chat is demoted only on positive evidence.
    assert _aged(0.0) == bands.chat_agent_oom_score_adj(
        is_open=False,
        is_visible=False,
        recency_rank=None,
        idle_seconds=None,
        is_mid_turn=False,
    )
