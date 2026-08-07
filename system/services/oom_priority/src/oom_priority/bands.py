"""Memory-shedding priority bands and the helper that writes them.

Each process is assigned to one band by writing its ``oom_score_adj`` once at
startup. earlyoom reads ``/proc/<pid>/oom_score`` (the kernel badness, which
already folds in ``oom_score_adj``) to pick its victim, so a higher band makes a
process more likely to be shed first.

Bands are positive-only. A negative ``oom_score_adj`` (true "never kill") would
require ``CAP_SYS_RESOURCE``, which the container's default capability set does
not grant; positive values still establish the relative ordering. The never-kill
infrastructure (sshd, supervisord, earlyoom itself, tini, and tmux) keeps the
inherited default of 0 -- nothing needs to tag it -- and is additionally shielded
by earlyoom ``--avoid``. The supervisord services (the UI, the tunnel, the
terminal, the backups, ...) are tagged into the low ``SERVICE_BANDS`` range so
they stay well below the agent bands while remaining strictly ordered among
themselves.

Raising a process's own (or a descendant's) ``oom_score_adj`` is unprivileged,
so the tagging hooks need no special capability.

This module is stdlib-only (see ``paths``): it is imported by the agent-tagging
and subprocess-tagging Claude hooks, which run under a plain ``python3``.
"""

from pathlib import Path
from typing import Final

# oom_score_adj value per band, most protected first. Tunable. Spaced so the
# ordering is unambiguous and there is room to interpose a band later.
PROTECTED: Final[int] = 0
# The workspace's primary (services) agent. Pinned to the same never-shed band as
# the infrastructure (``PROTECTED``): shedding it would tear down the workspace's
# supervised services and make it report a broken state, so it must outlive every
# other agent and service. Positive-only bands cannot express a true "never kill"
# (that needs ``CAP_SYS_RESOURCE``, which the container lacks), so this is the
# strongest available protection -- shed dead last, tied with sshd/supervisord.
PRIMARY_AGENT: Final[int] = PROTECTED
# A boundary marker, no longer assigned at launch: it equals ``CHAT_AGENT_FLOOR``
# (a maximally-engaged chat is as protected as a "user agent") and is the ceiling
# the service bands sit below. Every user-facing agent is a chat; there is no
# distinct plain-user-agent actor to tag with it.
USER_AGENT: Final[int] = 300
WORKER_AGENT: Final[int] = 600
AGENT_SUBPROCESS: Final[int] = 900

# Dynamic chat-agent band. A chat launches at ``CHAT_AGENT_BASE`` and is re-tagged
# at runtime from live activity (see the system_interface ``ChatOomPrioritizer``)
# anywhere within ``[CHAT_AGENT_FLOOR, CHAT_AGENT_STALE_CEILING]``:
#
#   300 CHAT_AGENT_FLOOR         a chat the user is engaged with right now
#   560 CHAT_AGENT_BASE          idle but fresh; also the chat launch band
#   600 WORKER_AGENT             (for reference -- not a chat band)
#   800 CHAT_AGENT_STALE_CEILING untouched long enough to be abandoned
#
# The range deliberately straddles ``WORKER_AGENT``. A chat the user last touched
# days ago is worth less than the worker their current chat just spawned: the chat
# revives on its next message with its transcript intact (only the next message
# pays a cold start, and history stays readable while it is down), whereas
# shedding a running worker destroys in-flight work. The ceiling still sits below
# ``AGENT_SUBPROCESS``, so any agent's build/test/browser subprocess is shed
# before any agent itself.
#
# Starting at ``CHAT_AGENT_BASE`` rather than the floor means a chat that is never
# re-tagged at all stays middling-expendable rather than pinned to the protected
# floor; only a positive staleness signal pushes one past the worker band.
CHAT_AGENT_BASE: Final[int] = 560  # idle but fresh; also the chat launch band
CHAT_AGENT_FLOOR: Final[int] = 300  # fully-engaged chat (most protected)
CHAT_AGENT_STALE_CEILING: Final[int] = 800  # abandoned chat (shed before a worker)

# --- Chat band tunables. Every knob of the chat policy lives in this block. ---
#
# How much each engagement signal protects a *fresh* chat, subtracted from
# ``CHAT_AGENT_BASE``. The recency bonus starts at its max for the most recently
# messaged chat and decays by one step per rank.
_CHAT_OPEN_BONUS: Final[int] = 80
_CHAT_VISIBLE_BONUS: Final[int] = 80
_CHAT_RECENCY_MAX_BONUS: Final[int] = 120
_CHAT_RECENCY_STEP: Final[int] = 15

_HOUR: Final[float] = 3600.0

# How a chat's freshness decays with idle time: ``(idle_seconds, freshness)``
# points in ascending idle order, linearly interpolated between neighbours and
# flat outside the ends. 1.0 is fully fresh (today's engagement-only behaviour),
# 0.0 fully abandoned (pinned to the stale ceiling regardless of engagement).
# Reshape the curve by editing, adding, or removing points here -- nothing else
# encodes the schedule.
_CHAT_FRESHNESS_RAMP: Final[tuple[tuple[float, float], ...]] = (
    (1 * _HOUR, 1.0),
    (24 * _HOUR, 0.0),
)


def _interpolate(x: float, points: tuple[tuple[float, float], ...]) -> float:
    """Piecewise-linear lookup of ``x`` over ascending ``(x, y)`` points.

    Flat outside the outermost points: below the first, the first ``y``; above
    the last, the last ``y``.
    """
    if x <= points[0][0]:
        return points[0][1]
    for (left_x, left_y), (right_x, right_y) in zip(points, points[1:]):
        if x <= right_x:
            span = right_x - left_x
            if span <= 0:
                return right_y
            return left_y + (right_y - left_y) * (x - left_x) / span
    return points[-1][1]


def _chat_freshness(idle_seconds: float | None, is_mid_turn: bool) -> float:
    """How fresh a chat counts as, in ``[0.0, 1.0]`` (see ``_CHAT_FRESHNESS_RAMP``).

    A chat that is mid-turn is always fully fresh: it is running work right now,
    and unlike an idle chat's revival that work is not recoverable, so age must
    not demote it however long ago it was last messaged. Unknown idle time (no
    engagement evidence at all) is also treated as fresh -- a chat is demoted only
    on positive evidence that it has been abandoned.
    """
    if is_mid_turn or idle_seconds is None:
        return 1.0
    return _interpolate(idle_seconds, _CHAT_FRESHNESS_RAMP)


def chat_agent_oom_score_adj(
    *,
    is_open: bool,
    is_visible: bool,
    recency_rank: int | None,
    idle_seconds: float | None,
    is_mid_turn: bool,
) -> int:
    """Map a chat agent's live activity to its ``oom_score_adj``.

    Lower is more protected. Two forces move a chat within its band, starting
    from ``CHAT_AGENT_BASE``. Engagement pulls it down:

    - ``is_open``: the chat has an open tab in the workspace UI.
    - ``is_visible``: the chat's tab is currently visible (implies open).
    - ``recency_rank``: this chat's position when the chats that have been
      messaged are sorted by last-message time, newest first (0 = most recently
      messaged). The bonus decays with rank, so more-recently-messaged chats are
      more protected than their peers. ``None`` means the chat has not been
      messaged (this session) and so gets no recency bonus -- a never-messaged
      chat must not be treated as if it were the most recent.

    Staleness pushes it up: ``idle_seconds`` (time since the chat was last
    engaged with by any route) scales the engagement bonuses down and lifts the
    score toward ``CHAT_AGENT_STALE_CEILING``. Because the same freshness factor
    scales both, engagement only ever *delays* the climb -- an abandoned chat ends
    up at the ceiling even with a visible tab, since a shed costs it nothing but a
    slow next message. ``is_mid_turn`` suspends the climb entirely (see
    ``_chat_freshness``).

    The result is clamped to ``[CHAT_AGENT_FLOOR, CHAT_AGENT_STALE_CEILING]``.
    """
    engagement_bonus = 0
    if is_open:
        engagement_bonus += _CHAT_OPEN_BONUS
    if is_visible:
        engagement_bonus += _CHAT_VISIBLE_BONUS
    if recency_rank is not None:
        engagement_bonus += max(
            0, _CHAT_RECENCY_MAX_BONUS - _CHAT_RECENCY_STEP * max(0, recency_rank)
        )
    freshness = _chat_freshness(idle_seconds, is_mid_turn)
    adj = round(
        CHAT_AGENT_BASE
        + (1.0 - freshness) * (CHAT_AGENT_STALE_CEILING - CHAT_AGENT_BASE)
        - freshness * engagement_bonus
    )
    return max(CHAT_AGENT_FLOOR, min(CHAT_AGENT_STALE_CEILING, adj))


# Supervisord service bands, keyed by the service key passed to
# ``system/services/oom_priority/bin/oom_tag_service.py``. Every value sits strictly between PROTECTED (0)
# and USER_AGENT (300), so a service is *less* expendable than any agent (an
# agent's work revives on the next message, so it is shed first) but still
# steerable relative to the other services.
#
# The services are ordered from least- to most-expendable by how much losing one
# hurts: the terminal (raw shell access) and the UI come first, then the sharing
# stack, then the runtime-state sync (github-sync, opt-in) and the host backup,
# then the app-watcher. ``user`` is the single band every *user-created* service
# shares; it sits above every built-in service so a user's own service is shed
# before any built-in one, while staying below USER_AGENT.
#
# sshd and the other never-kill infrastructure (supervisord, earlyoom, tini,
# tmux) are deliberately absent: they keep the inherited PROTECTED default (0)
# and are additionally shielded by earlyoom ``--avoid``.
#
# This is a best-effort steer, not a hard guarantee. earlyoom picks the highest
# ``/proc/*/oom_score``, which folds each process's live memory usage in on top
# of ``oom_score_adj``, so a large enough memory gap between two services can
# still reorder adjacent bands. The order only decides which service goes when
# earlyoom is forced to shed inside the protected pool -- i.e. once everything
# more expendable (browsers, agent subprocesses, agents, user services) is gone.
USER_SERVICE: Final[int] = 200
SERVICE_BANDS: Final[dict[str, int]] = {
    "terminal": 10,
    "system_interface": 20,
    # The sharing stack (gateway + caddy + frpc children inherit its band): a
    # shed share tunnel drops live viewers, so it sits just above the UI.
    "share-gateway": 35,
    # Opt-in runtime/ sync (added by the github-sync skill); inherits the band the
    # now-removed runtime-backup service used to hold.
    "github-sync": 40,
    "host-backup": 50,
    "app-watcher": 60,
    "user": USER_SERVICE,
}

# The shared-browser band: the absolute ceiling, one above AGENT_SUBPROCESS, so a
# browser always outranks even an agent's build/test subprocess and is shed first.
# The browser program tags itself at spawn (inline in its supervisord command,
# which cannot import this module); the constant exists so the backstop listener
# can re-assert the value if that self-tag failed.
SHARED_BROWSER: Final[int] = 1000

# The floor of the browser band's *range*. Chromium deliberately overwrites any
# inherited ``oom_score_adj`` once per process at startup with its own internal
# gradation (browser/zygote 0, gpu/utility 200, renderers 300 -- see
# ``AdjustLinuxOOMScore`` in chromium's chrome_main_delegate.cc), which without
# correction would leave the memory-heavy renderers *more* protected than the
# agents whose work they serve. The kernel offers no way to forbid that lowering
# without ``CAP_SYS_RESOURCE``, so the browser service instead sweeps its process
# tree and remaps every self-lowered value into ``[SHARED_BROWSER_FLOOR,
# SHARED_BROWSER]`` via ``shared_browser_oom_score_adj``: the whole browser tree
# stays above AGENT_SUBPROCESS, while Chromium's relative ordering (which is worth
# keeping -- shedding one renderer kills one tab, not the whole browser) is
# preserved in compressed form. Chromium only writes each value once, so a
# remapped value sticks.
SHARED_BROWSER_FLOOR: Final[int] = 910


def shared_browser_oom_score_adj(self_assigned: int) -> int:
    """Map a value Chromium assigned within its own 0..1000 gradation into the
    browser band's range ``[SHARED_BROWSER_FLOOR, SHARED_BROWSER]``.

    Order-preserving (a more-expendable Chromium process stays more expendable)
    and clamped, so any input lands inside the band range -- in particular at or
    above the floor, which is what makes the browser service's sweep idempotent
    (it only remaps values *below* the floor).
    """
    clamped = max(0, min(1000, self_assigned))
    span = SHARED_BROWSER - SHARED_BROWSER_FLOOR
    return SHARED_BROWSER_FLOOR + round(clamped * span / 1000)


# Expected band per supervisord program whose *program name* is not a
# SERVICE_BANDS key, for the backstop listener (system/services/oom_priority/bin/oom_tag_backstop.py).
# The OOM machinery itself (earlyoom, the listener) must stay PROTECTED -- it is
# what keeps every other band meaningful. deferred-install stays PROTECTED too:
# shedding the one-shot first-boot installer mid-run would leave provisioning
# half-done with no auto-restart to finish it.
_NON_SERVICE_PROGRAM_BANDS: Final[dict[str, int]] = {
    "earlyoom": PROTECTED,
    "oom-tag-backstop": PROTECTED,
    "deferred-install": PROTECTED,
    "browser": SHARED_BROWSER,
}


def supervisord_program_band(program_name: str) -> int:
    """The band a supervisord program is expected to occupy, by program name.

    A built-in service's program name doubles as its SERVICE_BANDS key; the
    handful of programs outside that map have explicit expected bands above.
    Anything unrecognized is a user-created service and falls back to
    ``USER_SERVICE``: an unknown process must default to being expendable, never
    to the protected default it would otherwise inherit.
    """
    if program_name in SERVICE_BANDS:
        return SERVICE_BANDS[program_name]
    return _NON_SERVICE_PROGRAM_BANDS.get(program_name, USER_SERVICE)


_PROC_DIR: Final[Path] = Path("/proc")


def _oom_score_adj_path(pid: int) -> Path:
    return _PROC_DIR / str(pid) / "oom_score_adj"


def read_oom_score_adj(pid: int) -> int | None:
    """Read ``pid``'s current ``oom_score_adj``, or None when it is unreadable
    (the process exited, or the host has no ``/proc`` -- e.g. macOS)."""
    try:
        return int(_oom_score_adj_path(pid).read_text().strip())
    except (OSError, ValueError):
        return None


def set_oom_score_adj(pid: int, adj: int) -> bool:
    """Write ``pid``'s ``oom_score_adj`` to ``adj``. Returns whether it stuck.

    A failure (the process exited, or the value is rejected) is reported via the
    return value rather than raised: callers are best-effort hooks that must not
    break the thing they are tagging.
    """
    try:
        _oom_score_adj_path(pid).write_text(f"{adj}\n")
    except OSError:
        return False
    return True
