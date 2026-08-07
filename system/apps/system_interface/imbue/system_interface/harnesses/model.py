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
from typing import Any

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.agent_discovery import AgentInfo


def parse_effort_level(value: Any) -> str | None:
    """Narrow a raw on-disk effort value to a non-empty string, or ``None``.

    Effort levels are free-form strings taken verbatim from each harness's own data
    (pi's thinking levels, codex's ``reasoning_effort``, claude's ``effortLevel``) --
    there is no fixed enum. This only checks the value is a non-empty string; whatever
    the harness's catalog declares for a model is what the picker shows.
    """
    return value if isinstance(value, str) and value else None


class EffortChoice(FrozenModel):
    """One effort in a model's declared set.

    ``level`` is a free-form string taken verbatim from the harness's catalog (pi's
    thinking levels, codex's reasoning efforts, claude's effort levels) -- never
    validated against a fixed enum, so a harness can offer whatever levels its own
    data declares (``off``/``minimal``/...)."""

    level: str
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
    # A free-form effort string from the harness's catalog, or None (a model with no
    # effort axis, or a live read before an effort was recorded).
    effort: str | None
    fast: bool


class ModelAxis(StrEnum):
    """One of the three independently-switchable axes of a model selection.

    The frontend sends exactly the axes a single click changed -- diffed against
    the value the user was looking at (the optimistic overlay), not against disk.
    A ``switch`` then applies only those axes, so re-picking the value you started
    on (medium -> xhigh -> medium) still sends ``/effort medium`` rather than being
    suppressed by a disk read that has not caught up yet, and an untouched axis is
    never re-issued.
    """

    MODEL = "model"
    EFFORT = "effort"
    FAST = "fast"


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


class PickerMode(StrEnum):
    """How the model dropdown renders its options. ONE value per harness, orthogonal
    to :class:`SwitchMode` -- that governs switching *behavior* (across model/effort/
    fast); this governs only the model picker's *presentation*. A five-model harness
    and a thousand-model harness need different affordances for identical behavior."""

    # Every option as a row (claude/codex -- small, hand-written catalogs).
    LIST = "list"
    # A search box filters the options by tag (pi/opencode -- huge, auth-gated sets).
    SEARCH = "search"


class HarnessCatalog(FrozenModel):
    """The serializable, per-harness static half. IS the ``/api/harnesses`` wire
    shape (dumped verbatim -- no endpoint-side field selection)."""

    # The catalog, in display order.
    options: tuple[ModelOption, ...]
    # Shown before config/disk says otherwise.
    default_model_id: str
    # One mode; applies to model, effort, AND fast.
    switch_mode: SwitchMode
    # How the model picker renders (list vs search); orthogonal to switch_mode.
    picker_mode: PickerMode
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


def merge_identities(live: ModelIdentity | None, guess: ModelIdentity | None) -> ModelIdentity | None:
    """Merge a live read over the guess, per field.

    Live wins where it has a value; the guess fills the one field a live read may
    leave unset (effort, before the harness has recorded one). ``model_id`` and
    ``fast`` come straight from the live read when it exists. When ``live`` is None the
    guess stands alone, and when BOTH are None (a harness with no launch default and
    nothing live yet -- pi before its model is recorded) the result is None, which the
    bar renders as logo-only rather than inventing a model.
    """
    if live is None:
        return guess
    effort = live.effort if live.effort is not None else (guess.effort if guess is not None else None)
    return ModelIdentity(model_id=live.model_id, effort=effort, fast=live.fast)


def to_options(entries: tuple[tuple[str, tuple[str, ...]], ...]) -> tuple[ModelOption, ...]:
    """Normalize ``(tag, effort_strings)`` pairs into catalog options.

    For the parsed catalogs (pi/opencode) that build their options from data rather
    than by hand. The tag is BOTH ``id`` and ``label`` (``provider/model``). Efforts
    are taken verbatim, in the order given (the source's own order). Neither pi nor
    opencode has a fast tier, so ``supports_fast`` is uniformly False. Duplicate tags
    collapse to the first -- the tag carries its provider prefix, so one model reachable
    through two providers is two genuine options, not a collision.
    """
    seen: set[str] = set()
    options: list[ModelOption] = []
    for tag, efforts in entries:
        if tag in seen:
            continue
        seen.add(tag)
        options.append(
            ModelOption(
                id=tag,
                label=tag,
                efforts=tuple(EffortChoice(level=level) for level in efforts),
                supports_fast=False,
            )
        )
    return tuple(options)


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
    def guess_from_launch(self) -> ModelIdentity | None:
        """The launch-config selection, read from the config file directly.

        Returns a concrete identity when the harness has a knowable launch model
        (claude, codex), or ``None`` when it does not (pi: many-auth, no default --
        the model only becomes known once its session records it). A returned identity
        should be fully concrete (effort filled from config or omitted for a
        no-effort model), so :func:`merge_identities` never leaves a field missing.
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

    def list_offered_models(self) -> tuple[str, ...] | None:
        """The model ids to OFFER in the picker right now, or None to offer the whole catalog.

        The catalog is the master list -- every model's label and thinking levels -- but it
        is not the offer set. For a harness whose offerable models are account-gated and
        dynamic (pi/opencode: only the providers/models the user is authenticated for), the
        picker calls this per open, so a fresh ``/login`` shows up without a catalog refetch.
        The returned ids are matched back against the catalog for their labels and efforts;
        ids absent from the catalog are simply not shown. The default -- for a small,
        static, non-gated catalog (claude, codex) -- returns None: offer everything.
        """
        return None

    @abstractmethod
    def switch(
        self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]
    ) -> SwitchResult:
        """Apply the ``axes`` of ``identity`` the caller says a click changed.

        ``axes`` is which of model/effort/fast to actually send -- computed on the
        frontend against the value the user saw, so the harness applies exactly
        those and never re-issues an untouched axis (and never suppresses a change
        just because disk has not caught up). The harness decides how -- it may
        validate first, then send one or many pane commands via ``send`` (bound by
        the endpoint to this agent). A display-only (:attr:`SwitchMode.READ_ONLY`)
        harness sends nothing and returns ``ok=False`` with a detail the endpoint
        maps to 409.
        """
