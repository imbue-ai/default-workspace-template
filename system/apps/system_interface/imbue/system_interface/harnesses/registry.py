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
from enum import StrEnum
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


class PopupTrigger(StrEnum):
    """When a declared popup fires. Values are the wire strings the frontend matches."""

    # Matches a typed message's first token against the popup's commands at send time.
    COMPOSER_COMMAND = "composer_command"
    # Runs on every chat render -- the fast-mode grace-period check.
    TURN_CHECK = "turn_check"


class PopupAction(StrEnum):
    """What the frontend does when a declared popup fires. Values are wire strings."""

    # The can't-send-from-chat notice.
    NOTICE = "notice"
    # Open the harness's agent-auth surface (see ``HarnessSpec.auth_modal``).
    OPEN_AUTH = "open_auth"
    # The keep-fast-mode prompt flow.
    FAST_MODE_PROMPT = "fast_mode_prompt"


class AuthModalKind(StrEnum):
    """Which agent-auth surface a harness's ``open_auth`` popups open. Wire strings."""

    # The in-app login modal (claude -- its auth is mind-global).
    MANAGED = "managed"
    # A notice showing the harness's ``auth_instructions``, for harnesses whose
    # sign-in runs in their own TUI.
    TERMINAL = "terminal"


class HarnessPopup(FrozenModel):
    """One popup a harness declares for the chat UI.

    Shipped to the frontend inside the ``/api/harnesses`` payload, so the composer and
    chat panel act on whatever the agent's harness declared instead of branching on the
    harness name.
    """

    trigger: PopupTrigger
    commands: tuple[str, ...] = ()
    action: PopupAction
    # NOTICE only: replaces the notice's default "send it from the agent's terminal"
    # body. A harness declares WHY a command is declined; the composer stays free of
    # per-command branching, which is the whole point of shipping these declaratively.
    notice_body: str | None = None


# The three commands the model bar owns, declined on EVERY harness for a different
# reason than the lists below: not that they break the terminal, but that the composer
# is not where a model change belongs. Typing one works, which is exactly the problem --
# the message is stamped hidden (it is the bar's own traffic), so the model silently
# changes with nothing in the transcript to show it, and a typed /fast additionally does
# not persist, because only the bar's switch path records fastMode where a restart reads
# it back. Kept as its own popup rather than folded into a harness's declined tuple so
# the distinct rationale survives: those tuples are measured-against-a-live-agent lists,
# and a future re-measure would find these three send fine and drop them.
_MODEL_BAR_COMMANDS: Final[tuple[str, ...]] = ("/model", "/effort", "/fast")
_MODEL_BAR_NOTICE: Final[str] = "Use the model picker below the chat to change the model, effort, or speed."
_MODEL_BAR_POPUP: Final[HarnessPopup] = HarnessPopup(
    trigger=PopupTrigger.COMPOSER_COMMAND,
    commands=_MODEL_BAR_COMMANDS,
    action=PopupAction.NOTICE,
    notice_body=_MODEL_BAR_NOTICE,
)


# Claude Code slash commands the chat declines to send (action="notice").
#
# A chat message reaches a claude agent by being typed into its terminal, so a command
# that changes the terminal rather than starting a turn does something the chat cannot
# undo. Most of these replace Claude Code's input box with a full-pane view, after which
# the agent cannot accept any further message until the view is dismissed; /exit (and
# its alias /quit) shuts the session down outright. Either way the command still works
# from the agent's terminal, which is what the notice points the user at.
#
# Every entry was measured against claude 2.1.220 by sending it to a live agent and
# confirming both that the input box was gone afterwards and that a following message
# failed to send. Alias spellings sit alongside the command they resolve to (/cost and
# /stats are /usage, /settings is /config, /allowed-tools is /permissions, /bashes is
# /tasks, /quit is /exit) -- not duplicates. Matching is by first token, so every
# argument form is declined with its command: deliberately over-declining, because a
# declined form that would have worked costs one trip to the terminal while an allowed
# form that takes over the pane leaves the agent unable to answer in chat.
_CLAUDE_DECLINED_COMMANDS: Final[tuple[str, ...]] = (
    "/add-dir",
    "/allowed-tools",
    "/bashes",
    "/config",
    "/cost",
    "/diff",
    "/exit",
    "/extra-usage",
    "/goal",
    "/help",
    "/hooks",
    "/ide",
    "/mcp",
    "/permissions",
    "/powerup",
    "/privacy-settings",
    "/quit",
    "/release-notes",
    "/settings",
    "/skills",
    "/stats",
    "/status",
    "/tasks",
    # Here for its argument form only: bare /theme sends fine, /theme dark takes over.
    "/theme",
    "/usage",
    "/usage-credits",
    "/workflows",
)

# Codex slash commands the chat declines to send. Codex messages are delivered
# programmatically over the app-server (turn/start), never typed into the pane, so a
# slash command sent from chat would reach the model as literal prose -- and /model,
# /fast, and /effort are additionally stamped hidden by the shared display rules (they
# are the claude model bar's typed commands), so the user's message would silently
# vanish from the transcript. The notice points every one of these at the terminal,
# where codex's own TUI handles them.
_CODEX_DECLINED_COMMANDS: Final[tuple[str, ...]] = (
    "/archive",
    "/btw",
    "/clear",
    "/delete",
    "/exit",
    "/experimental",
    "/fork",
    "/keymap",
    "/new",
    "/plan",
    "/quit",
    "/resume",
    "/side",
    "/vim",
)

# Pi slash commands the chat declines to send. pi is driven through its lifecycle
# extension's inbox rather than by typing into the pane, so these would reach the
# model as literal prose; the ones that switch or discard a session (/new, /resume,
# /fork, /clone, /compact, /quit) would also strand the chat's view of the
# conversation if they ever did run. The notice points them at the terminal.
_PI_DECLINED_COMMANDS: Final[tuple[str, ...]] = (
    "/clone",
    "/compact",
    "/fork",
    "/llama",
    "/name",
    "/new",
    "/quit",
    "/resume",
    "/scoped-models",
    "/session",
    "/tree",
)

# The auth-intercept commands every auth-declaring harness shares: typing either opens
# the harness's agent-auth surface instead of sending.
_AUTH_COMMANDS: Final[tuple[str, ...]] = ("/login", "/logout")

# The fast-mode grace-period prompt, declared by the harnesses that can launch fast.
_FAST_MODE_PROMPT_POPUP: Final[HarnessPopup] = HarnessPopup(
    trigger=PopupTrigger.TURN_CHECK, action=PopupAction.FAST_MODE_PROMPT
)


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
    # How to tell whether this harness's CLI is signed in before creating an agent on it.
    # ``None`` = no auth gate (claude's auth lives in the shared ``~/.claude``).
    auth_check: HarnessAuthCheck | None = None
    # The popups this harness declares for the chat UI, shipped on the wire with the
    # catalog. An empty tuple is the honest statement that a harness has none (pi's
    # composer sends everything as-is and it never launches fast).
    popups: tuple[HarnessPopup, ...] = ()
    # The user-facing agent-auth surface the ``open_auth`` popup action (and the stream
    # auth-error hook) opens; ``terminal`` surfaces show ``auth_instructions``.
    auth_modal: AuthModalKind = AuthModalKind.TERMINAL
    auth_instructions: str | None = None


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
        popups=(
            HarnessPopup(trigger=PopupTrigger.COMPOSER_COMMAND, commands=_AUTH_COMMANDS, action=PopupAction.OPEN_AUTH),
            HarnessPopup(
                trigger=PopupTrigger.COMPOSER_COMMAND, commands=_CLAUDE_DECLINED_COMMANDS, action=PopupAction.NOTICE
            ),
            _MODEL_BAR_POPUP,
            _FAST_MODE_PROMPT_POPUP,
        ),
        auth_modal=AuthModalKind.MANAGED,
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
        popups=(
            HarnessPopup(trigger=PopupTrigger.COMPOSER_COMMAND, commands=_AUTH_COMMANDS, action=PopupAction.OPEN_AUTH),
            HarnessPopup(
                trigger=PopupTrigger.COMPOSER_COMMAND, commands=_CODEX_DECLINED_COMMANDS, action=PopupAction.NOTICE
            ),
            _MODEL_BAR_POPUP,
            _FAST_MODE_PROMPT_POPUP,
        ),
        auth_instructions=("Open the agent's terminal and run /logout, then /login, to sign in or switch accounts."),
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
        popups=(
            HarnessPopup(trigger=PopupTrigger.COMPOSER_COMMAND, commands=("/login",), action=PopupAction.OPEN_AUTH),
            HarnessPopup(
                trigger=PopupTrigger.COMPOSER_COMMAND, commands=_PI_DECLINED_COMMANDS, action=PopupAction.NOTICE
            ),
            _MODEL_BAR_POPUP,
        ),
        auth_instructions="Open the agent's terminal and run /login to add accounts or keys.",
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
