"""The shared model spine: the model-bar's harness-neutral types, the resolver
interface every harness implements, and the one matcher both validation and
display agree on.

The model bar renders ``[Logo][Model][Effort][Fast]`` from two data sources kept
strictly apart:

* the **catalog** (:class:`HarnessCatalog`) -- static, per-harness, compile-time:
  which models exist, their labels, which efforts each declares (and which of
  those show in the picker), whether each supports fast, the harness's switch
  mode, and its logo. Served once via ``GET /api/harnesses``.
* the **choice** (:class:`ModelChoice`) -- live, per-agent, runtime: which
  ``(model, effort, fast)`` one agent is on, its provenance, and the catalog
  option it matched. Rides the agents WebSocket beside ``activity_state``.

Adding a harness is one :class:`HarnessCatalog` + one :class:`HarnessModelResolver`
subclass + one registry entry -- nothing here changes. The resolver is the model
analogue of :class:`~imbue.system_interface.harnesses.activity.HarnessActivityTracker`:
AgentManager owns one per tracked agent, built from the agent's harness, and calls
it instead of branching on the harness name.
"""

from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.agent_discovery import AgentInfo


class EffortLevel(StrEnum):
    """The full universe of reasoning-effort levels.

    A harness's declared efforts are a subset of these, and the levels it *shows*
    in the picker are a further subset (see :class:`EffortChoice`). Declaring the
    full universe lets a live read of a rarely-shown level (claude's ``ultra`` /
    ultracode) still match its option and display, even though the picker hides it.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class EffortChoice(FrozenModel):
    """One effort in a model's declared set."""

    level: EffortLevel
    # False = a valid, matchable level that is nonetheless hidden from the dropdown.
    in_picker: bool = True


class ModelOption(FrozenModel):
    """One model in a harness's catalog. Static -- never per-agent."""

    # What ``switch()`` sends and what a live read is matched against (e.g.
    # ``opus[1m]``, ``gpt-5.6-sol``).
    id: str
    # Human name shown in the bar (e.g. ``Opus 5 (1M)``, ``GPT-5.6-Sol``).
    label: str
    # The DECLARED effort set: the validity + matching universe for this model.
    # An empty tuple means the model has no effort axis (the slot is hidden).
    efforts: tuple[EffortChoice, ...]
    # Whether fast mode applies to this model. Per-MODEL, not per-harness.
    supports_fast: bool
    # False = a hidden model: matchable (a live read of it still displays) but not
    # offered in the dropdown.
    in_picker: bool = True


class ModelIdentity(FrozenModel):
    """The tuple that IS a selection. Resolvers return it; ``switch()`` sets it."""

    model_id: str
    # None only mid-merge (a live read before the harness has recorded an effort);
    # a resolved choice handed to the frontend is always concrete (see
    # :func:`merge_identities`).
    effort: EffortLevel | None
    fast: bool


class ModelChoiceSource(StrEnum):
    """Where a :class:`ModelChoice` came from. The backend emits only these two;
    the frontend adds a local ``pending`` for an optimistic pick."""

    # From launch config, before the first turn.
    GUESS = "guess"
    # Read from disk after the harness wrote real state.
    LIVE = "live"


class ModelChoice(FrozenModel):
    """The live, per-agent selection sent to the browser. The runtime half.

    ``matched`` is the catalog option the identity resolved to, computed once on
    the backend (see :func:`match_option`) so the frontend never re-matches. It is
    ``None`` when the identity matches no catalog option -- the ``shrug`` case,
    where the bar shows no model/effort/fast slots.
    """

    identity: ModelIdentity
    source: ModelChoiceSource
    matched: ModelOption | None


class SwitchMode(StrEnum):
    """How a harness's model bar behaves. ONE value per harness; it governs the
    model, effort, and fast axes uniformly. It has nothing to do with which axes
    are *shown* -- that is decided purely by the matched model's data."""

    # Optimistic: the chip moves on click, then reconciles from disk.
    EAGER_THEN_RECONCILE = "eager_then_reconcile"
    # Switchable but not optimistic: the chip updates only once disk reflects it.
    ON_CHANGE = "on_change"
    # Display only: the slots show the current value but are not interactive.
    READ_ONLY = "read_only"


class HarnessCatalog(FrozenModel):
    """The serializable, per-harness static half. IS the ``/api/harnesses`` wire
    shape (dumped verbatim -- no endpoint-side field selection)."""

    # The catalog, in display order.
    options: tuple[ModelOption, ...]
    # Shown before config/disk says otherwise.
    default_model_id: str
    # One mode; applies to model, effort, AND fast.
    switch_mode: SwitchMode
    # Harness logo, currentColor monochrome.
    icon_svg: str


class SwitchResult(FrozenModel):
    """The outcome of a :meth:`HarnessModelResolver.switch`. ``ok=False`` carries a
    ``detail`` the endpoint surfaces to the user."""

    ok: bool
    detail: str | None = None


def base_alias(model: str) -> str:
    """Reduce a model string to its bare alias for matching.

    Harnesses stamp context/variant suffixes onto the alias (claude's
    ``opus[1m]``), so stripping the ``[...]`` suffix lets a stored ``opus`` or
    ``opus[1m]`` both match the catalog's Opus option. Harness-neutral: a model id
    with no suffix (codex's ``gpt-5.6-sol``) is returned unchanged (lowercased).
    """
    return model.split("[", 1)[0].strip().lower()


def match_option(identity: ModelIdentity, options: tuple[ModelOption, ...]) -> ModelOption | None:
    """The catalog option ``identity`` resolves to, or None (the shrug case).

    Matches iff the model aliases agree, the identity's effort is declared by the
    option (or is None, which a no-effort model requires), and fast is not on for a
    model that does not support it. Uses the full declared effort set, so a
    live-read hidden level still matches. One implementation, shared by the
    ``POST /model`` validation and the pushed :class:`ModelChoice`.
    """
    alias = base_alias(identity.model_id)
    for option in options:
        if base_alias(option.id) != alias:
            continue
        declared = {choice.level for choice in option.efforts}
        if identity.effort is not None and identity.effort not in declared:
            continue
        if identity.fast and not option.supports_fast:
            continue
        return option
    return None


def merge_identities(live: ModelIdentity | None, guess: ModelIdentity) -> ModelIdentity:
    """Merge a live read over the always-concrete guess, per field.

    Live wins where it has a value; the guess fills the one field a live read may
    leave unset (effort, before the harness has recorded one). ``model_id`` and
    ``fast`` come straight from the live read when it exists. When ``live`` is
    None (nothing on disk yet), the guess stands alone.
    """
    if live is None:
        return guess
    effort = live.effort if live.effort is not None else guess.effort
    return ModelIdentity(model_id=live.model_id, effort=effort, fast=live.fast)


class HarnessModelResolver(ABC):
    """Resolves and (for a switchable harness) applies ONE agent's model choice.

    Two reads plus one write, all harness-specific and all contained to the
    harness's ``model.py``:

    * :meth:`guess_from_launch` -- the pre-turn selection from launch config
    * :meth:`read_live` -- the current on-disk selection, or None when disk is
      silent so far
    * :meth:`switch` -- apply a selection (a no-op for a display-only harness)

    plus :meth:`watched_paths`, which names the files/dirs whose change means a
    fresh :meth:`read_live` may differ, driving the live recompute.

    ``build`` takes the whole :class:`AgentInfo` (like the watcher) so each harness
    reads the paths IT needs and the caller never learns which.
    """

    @classmethod
    @abstractmethod
    def build(cls, agent_info: AgentInfo) -> "HarnessModelResolver":
        """Construct for one agent, not yet reading anything."""

    @abstractmethod
    def guess_from_launch(self) -> ModelIdentity:
        """The launch-config selection, read from the config file directly.

        ALWAYS returns a fully concrete identity -- effort is the config value or
        the harness's declared default, never None -- so the merged choice is never
        missing a field and the chip needs no default-label special-case.
        """

    @abstractmethod
    def read_live(self) -> ModelIdentity | None:
        """The current on-disk selection, or None when disk has recorded nothing.

        Individual fields MAY be None (claude's effort before the first
        ``/effort``); :func:`merge_identities` fills those from the guess. Returning
        None for the whole identity means "nothing live yet, use the guess".
        """

    @abstractmethod
    def watched_paths(self) -> tuple[Path, ...]:
        """Files/dirs whose change means :meth:`read_live` may now differ.

        Drives the sole live recompute trigger. A path that does not exist yet is
        fine (the watcher retries once it appears). A path that is a directory is
        watched recursively; a file is watched via its parent directory.
        """

    @abstractmethod
    def switch(self, identity: ModelIdentity, send: Callable[[str], bool]) -> SwitchResult:
        """Apply ``identity``. The harness decides how -- it may validate first,
        then send one or many pane commands via ``send`` (bound by the endpoint to
        this agent). A display-only (:attr:`SwitchMode.READ_ONLY`) harness sends
        nothing and returns ``ok=False`` with a detail the endpoint maps to 409.
        """
