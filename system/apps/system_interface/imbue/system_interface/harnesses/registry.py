"""The one place a harness is registered.

A harness is resolved EXACTLY ONCE, in :mod:`agent_discovery`, from mngr's
``AgentDetails.type``, and carried on ``AgentInfo.harness``. Every component downstream
receives an already-resolved object from this module and never re-derives, re-checks, or
branches on the harness name.

Adding a harness is one :class:`HarnessSpec` entry here, one subclass per concern, and --
if it emits markers of its own -- one member of
:class:`~imbue.system_interface.harnesses.events.SpecialEventKind`. Nothing else changes:
``app_context`` builds watchers through :func:`build_watcher`, ``agent_manager`` builds
trackers through :func:`build_tracker`, and neither names a harness.

One spec per harness rather than a dict per concern, deliberately: parallel registries
would mean two places to edit for one harness, which is how the split drifts.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.activity import HarnessActivityTracker
from imbue.system_interface.harnesses.auth_check import CODEX_AUTH_CHECK
from imbue.system_interface.harnesses.auth_check import HarnessAuthCheck
from imbue.system_interface.harnesses.auth_check import PI_AUTH_CHECK
from imbue.system_interface.harnesses.claude.activity import ClaudeActivityTracker
from imbue.system_interface.harnesses.claude.model import CLAUDE_CATALOG
from imbue.system_interface.harnesses.claude.model import CLAUDE_STATE_RELATIVE_PATH
from imbue.system_interface.harnesses.claude.model import ClaudeModelResolver
from imbue.system_interface.harnesses.claude.tap import ClaudeAtomicShoulderTap
from imbue.system_interface.harnesses.claude.tap import ClaudeInterruptToComposer
from imbue.system_interface.harnesses.claude.watcher import ClaudeSessionWatcher
from imbue.system_interface.harnesses.codex.activity import CodexActivityTracker
from imbue.system_interface.harnesses.codex.model import CODEX_CATALOG
from imbue.system_interface.harnesses.codex.model import CODEX_STATE_RELATIVE_PATH
from imbue.system_interface.harnesses.codex.model import CodexModelResolver
from imbue.system_interface.harnesses.codex.session import CodexHarnessSession
from imbue.system_interface.harnesses.codex.watcher import CodexSessionWatcher
from imbue.system_interface.harnesses.events import SpecialEventKind
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.interrupt import InterruptToComposer
from imbue.system_interface.harnesses.interrupt import RestartDrainInterruptToComposer
from imbue.system_interface.harnesses.model import HarnessCatalog
from imbue.system_interface.harnesses.model import HarnessModelResolver
from imbue.system_interface.harnesses.model import model_state_path
from imbue.system_interface.harnesses.pi_coding.activity import PiActivityTracker
from imbue.system_interface.harnesses.pi_coding.model import PI_STATE_RELATIVE_PATH
from imbue.system_interface.harnesses.pi_coding.model import PiAtomicShoulderTap
from imbue.system_interface.harnesses.pi_coding.model import PiInterruptToComposer
from imbue.system_interface.harnesses.pi_coding.model import PiModelResolver
from imbue.system_interface.harnesses.pi_coding.model import get_catalog as get_pi_catalog
from imbue.system_interface.harnesses.pi_coding.watcher import PiSessionWatcher
from imbue.system_interface.harnesses.session import AgentHarnessSession
from imbue.system_interface.harnesses.session import AtomicShoulderTap
from imbue.system_interface.harnesses.session import FileHarnessSession
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback


class HarnessSpec(FrozenModel):
    """Everything the system interface needs to run one harness."""

    # The watcher/tracker fields hold CLASSES, which pydantic cannot validate structurally.
    model_config = {"arbitrary_types_allowed": True}

    name: HarnessType
    watcher_class: type[AgentSessionWatcher]
    # The transcript-derived activity tracker. Every harness has one -- claude/pi infer the
    # turn from the transcript tail, codex latches its explicit turn markers.
    tracker_class: type[HarnessActivityTracker]
    # The model resolver class -- a true peer of watcher_class/tracker_class, so it
    # sits flat here and AgentManager calls ``.build(agent_info)`` on it the same way.
    resolver_class: type[HarnessModelResolver]
    # Where the harness writes its uniform ``model_state.json``, RELATIVE to the agent
    # state dir -- the one per-harness difference the shared live read/watch takes as data.
    # ``Path(".")`` = the state-dir root (claude, pi); codex writes under its CODEX_HOME.
    model_state_relative_path: Path
    # A factory for the per-harness model catalog, called (once, cached) by ``get_catalog``.
    # Behind a factory rather than a value because pi PARSES thousands of models off
    # disk -- too much for import time, and importing this module must not fail on an image
    # where a harness's data is absent. claude/codex just return their hand-written constant.
    catalog_factory: Callable[[], HarnessCatalog]
    # The special-event kinds this harness may emit. A parser emitting a kind outside its
    # own declaration is a bug; an empty set is the honest statement that a harness's
    # transcript carries no markers, not an omission.
    special_kinds: frozenset[SpecialEventKind]
    # The stop-button (interrupt-to-composer) implementation. Defaults to the base restart-drain
    # (SIGKILL-relaunch) that claude and any future harness use; a harness that can interrupt its
    # live turn natively registers an override (pi does). Backend-only: no wire-visible flag.
    interrupt_to_composer_class: type[InterruptToComposer] = RestartDrainInterruptToComposer
    # The live control surface (send + Sending state, tap, interrupt dispatch, model options).
    # The file-API default covers claude and pi; a harness driven through a live daemon
    # connection registers its own (codex).
    session_class: type[AgentHarnessSession] = FileHarnessSession
    # The native atomic shoulder tap, the tap peer of ``interrupt_to_composer_class``. File
    # harnesses that advertise ``native_atomic_shoulder_tap_possible`` register one; a harness
    # that taps through its live connection (codex) registers none and overrides
    # ``shoulder_tap`` on its session directly.
    shoulder_tap_class: type[AtomicShoulderTap] | None = None
    # Extra ``mngr create`` args for this harness, given whether fast mode is enabled. Chat is
    # the one interactive agent type, so it is the only one that starts fast; fast mode is a
    # claude setting, so every other harness contributes nothing.
    launch_settings_overrides: Callable[[bool], tuple[str, ...]] = lambda _is_fast: ()
    # How to tell whether this harness's CLI is signed in before creating an agent on it.
    # ``None`` = no auth gate (claude's auth lives in the shared ``~/.claude``).
    auth_check: HarnessAuthCheck | None = None


HARNESS_SPECS: Final[dict[HarnessType, HarnessSpec]] = {
    HarnessType.CLAUDE: HarnessSpec(
        name=HarnessType.CLAUDE,
        watcher_class=ClaudeSessionWatcher,
        tracker_class=ClaudeActivityTracker,
        resolver_class=ClaudeModelResolver,
        catalog_factory=lambda: CLAUDE_CATALOG,
        model_state_relative_path=CLAUDE_STATE_RELATIVE_PATH,
        # Claude Code's transcript has no turn boundaries; activity is inferred from an
        # unmatched tool_use plus the transcript tail.
        special_kinds=frozenset(),
        # claude interrupts an EMPTY-queue turn natively via the meta+q cancel chord (a pure
        # interrupt, confirm-then-clear of the stranded active marker); a NONEMPTY queue keeps
        # the base restart-drain. See harnesses/claude/tap.py.
        interrupt_to_composer_class=ClaudeInterruptToComposer,
        shoulder_tap_class=ClaudeAtomicShoulderTap,
        launch_settings_overrides=lambda is_fast: (
            "-S",
            f"agent_types.claude.settings_overrides.fastMode={str(is_fast).lower()}",
        ),
    ),
    HarnessType.CODEX: HarnessSpec(
        name=HarnessType.CODEX,
        watcher_class=CodexSessionWatcher,
        # The dot is a latch on the transcript's turn markers (task_started/task_complete in the
        # rollout); the mngr lifecycle is deliberately NOT consulted -- it is polled, hence laggy
        # and unreliable for codex (see harnesses/codex/activity.py). (The old design drove the dot
        # from the ledger's turn/started..turn/completed app-server events, but codex no longer
        # emits those, only thread/status/changed, so the dot got stuck. The ledger stays as the
        # queue/message-lifecycle authority; it does not drive the dot.)
        tracker_class=CodexActivityTracker,
        resolver_class=CodexModelResolver,
        catalog_factory=lambda: CODEX_CATALOG,
        model_state_relative_path=CODEX_STATE_RELATIVE_PATH,
        special_kinds=frozenset(
            {
                SpecialEventKind.TURN_STARTED,
                SpecialEventKind.TURN_COMPLETED,
                SpecialEventKind.TURN_ABORTED,
            }
        ),
        # codex's stop button and tap go through its session's live ledger (one
        # ``turn/interrupt`` + a per-id settle / the combined early resend), so the registered
        # interrupter default is inert and no shoulder_tap_class is needed.
        session_class=CodexHarnessSession,
        auth_check=CODEX_AUTH_CHECK,
    ),
    HarnessType.PI_CODING: HarnessSpec(
        name=HarnessType.PI_CODING,
        # Tails pi's native session JSONL (via the pi_session_file marker) and populates the
        # queue from mngr's pi_inbox. pi's transcript carries no turn markers (like claude),
        # so activity is the lifecycle-plus-tail heuristic.
        watcher_class=PiSessionWatcher,
        tracker_class=PiActivityTracker,
        resolver_class=PiModelResolver,
        catalog_factory=get_pi_catalog,
        model_state_relative_path=PI_STATE_RELATIVE_PATH,
        special_kinds=frozenset(),
        # pi interrupts natively via the lifecycle extension (retract sentinel on pi_inbox), so
        # it overrides the base restart-drain rather than SIGKILL-relaunching.
        interrupt_to_composer_class=PiInterruptToComposer,
        shoulder_tap_class=PiAtomicShoulderTap,
        auth_check=PI_AUTH_CHECK,
    ),
}


def get_harness_spec(harness: HarnessType) -> HarnessSpec:
    """The spec for ``harness``. Total: every member has one, checked by ``test_every_harness_has_a_spec``."""
    return HARNESS_SPECS[harness]


def build_watcher(agent_info: AgentInfo, on_events: OnEventsCallback) -> AgentSessionWatcher:
    """Build the session watcher for ``agent_info``'s harness, not yet started."""
    return get_harness_spec(agent_info.harness).watcher_class.build(agent_info, on_events)


def build_tracker(harness: HarnessType) -> HarnessActivityTracker:
    """Build the activity tracker for ``harness``."""
    return get_harness_spec(harness).tracker_class.build()


def build_shoulder_tap(agent_info: AgentInfo) -> AtomicShoulderTap | None:
    """Build the native atomic shoulder tap for ``agent_info``'s harness, or ``None`` when the
    harness registers none (its session taps through a live connection instead)."""
    tap_class = get_harness_spec(agent_info.harness).shoulder_tap_class
    return tap_class.build(agent_info) if tap_class is not None else None


def build_resolver(agent_info: AgentInfo) -> HarnessModelResolver:
    """Build the model resolver for ``agent_info``'s harness."""
    return get_harness_spec(agent_info.harness).resolver_class.build(agent_info)


def build_interrupt_to_composer(agent_info: AgentInfo) -> InterruptToComposer:
    """Build the stop-button (interrupt-to-composer) implementation for ``agent_info``'s harness."""
    return get_harness_spec(agent_info.harness).interrupt_to_composer_class.build(agent_info)


def get_model_state_path(harness: HarnessType, agent_state_dir: Path) -> Path:
    """The agent's live ``model_state.json`` -- the file the shared reader parses and
    the model watcher watches -- under its harness's registered relative directory.

    Takes the harness + state dir (not the whole ``AgentInfo``) so the hot recompute path
    need not resolve ``claude_config_dir``, which costs an extra env-file read it never uses.
    """
    return model_state_path(agent_state_dir, get_harness_spec(harness).model_state_relative_path)


# Built once per process from each harness's factory. The parsed catalogs (pi)
# read data files that ship in the image, so a non-empty catalog is immutable for the
# container's life and cached unconditionally; an empty one (harness data absent) is NOT
# cached, so a later call retries rather than freezing the blank.
_CATALOG_CACHE: dict[HarnessType, HarnessCatalog] = {}


def get_catalog(harness: HarnessType) -> HarnessCatalog:
    """The model catalog for ``harness``, built via its factory and cached when non-empty."""
    cached = _CATALOG_CACHE.get(harness)
    if cached is not None:
        return cached
    catalog = get_harness_spec(harness).catalog_factory()
    if catalog.options:
        _CATALOG_CACHE[harness] = catalog
    return catalog
