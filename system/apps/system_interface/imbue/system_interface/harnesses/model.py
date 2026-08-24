"""The shared model spine: the model-bar's harness-neutral types, the resolver
interface every harness implements, and the one matcher both validation and
display agree on.

The model bar renders ``[Model][Effort][Fast]`` from two data sources kept
strictly apart:

* the **catalog** (:class:`HarnessCatalog`) -- static, per-harness, compile-time:
  which models exist, their labels, which efforts each declares (and which of
  those show in the picker), whether each supports fast, the harness's switch
  mode, and its "powered by" credit label. Served once via ``GET /api/harnesses``.
* the **choice** (:class:`ModelChoice`) -- live, per-agent, runtime: which
  ``(model, effort, fast)`` one agent is on and the catalog option it matched.
  Rides the agents WebSocket beside ``activity_state``.

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
from imbue.mngr.utils.file_utils import read_json_dict
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

    # What ``switch()`` sends (e.g. ``opus[1m]``, ``gpt-5.6-sol``).
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
    # The raw model id the harness reports in its live state file, matched against a
    # live read (:func:`match_option`). ``None`` means "same as ``id``" -- for a harness
    # whose reported id equals its switch id (codex, pi). Claude reports an API id
    # (``claude-opus-5``) that differs from the ``[1m]``-suffixed switch id, so its
    # options set this explicitly.
    harness_reported_model_id: str | None = None


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


class ModelChoice(FrozenModel):
    """The live, per-agent selection sent to the browser. The runtime half.

    ``matched`` is the catalog option the identity resolved to, computed once on
    the backend (see :func:`match_option`) so the frontend never re-matches. It is
    ``None`` when the identity matches no catalog option -- the ``shrug`` case,
    where the bar shows no model/effort/fast slots.
    """

    identity: ModelIdentity
    matched: ModelOption | None


class SwitchMode(StrEnum):
    """How a harness's model bar behaves. ONE value per harness; it governs the
    model, effort, and fast axes uniformly. It has nothing to do with which axes
    are *shown* -- that is decided purely by the matched model's data. claude and pi
    use EAGER_THEN_RECONCILE; codex uses ON_CHANGE."""

    # Optimistic: the chip moves on click, then reconciles from disk.
    EAGER_THEN_RECONCILE = "eager_then_reconcile"
    # Interactive but NOT optimistic: the switch is a fast app-server request whose confirmed
    # effective settings are pushed straight back as the authoritative ModelChoice, so the chip
    # moves on the CONFIRMED change (a beat later, no overlay), never on the raw click. Codex uses
    # it -- thread/settings/update round-trips in well under a second, so waiting for the pushed
    # choice reads as instant while never showing a value the daemon has not accepted. The frontend
    # derives ``optimistic = switch_mode === "eager_then_reconcile"``, so this yields no overlay.
    ON_CHANGE = "on_change"
    # Display-only: the bar REFLECTS the harness's model and cannot drive it. The frontend
    # renders the slots non-interactive (a readonly trigger + "use the agent terminal"
    # tooltip) and never opens a picker. antigravity uses it: agy's `/model` is an
    # interactive TUI picker with no scriptable one-shot form, and its `--model` flag applies
    # only at launch, so there is no mid-session switch to offer.
    READ_ONLY = "read_only"


class PickerMode(StrEnum):
    """How the model dropdown renders its options. ONE value per harness, orthogonal
    to :class:`SwitchMode` -- that governs switching *behavior* (across model/effort/
    fast); this governs only the model picker's *presentation*. A five-model harness
    and a thousand-model harness need different affordances for identical behavior."""

    # Every option as a row (claude -- a small, hand-written catalog).
    LIST = "list"
    # A search box filters the options by tag (pi -- huge, auth-gated sets).
    SEARCH = "search"
    # A LIST-rendered picker whose OPTIONS are per-agent, fetched live from the model-options
    # endpoint (not the static catalog, which is empty). Codex uses it: its model set, each
    # model's efforts, and fast support all come from the daemon's ``model/list``, so there is
    # no static list to render -- the picker sources the full options per open (D2: always fresh).
    DYNAMIC = "dynamic"


class HarnessCatalog(FrozenModel):
    """The serializable, per-harness static half. IS the ``/api/harnesses`` wire
    shape (dumped verbatim -- no endpoint-side field selection)."""

    # The catalog, in display order.
    options: tuple[ModelOption, ...]
    # One mode; applies to model, effort, AND fast.
    switch_mode: SwitchMode
    # How the model picker renders (list vs search); orthogonal to switch_mode.
    picker_mode: PickerMode
    # The exact, non-clickable credit text shown beside the composer's "Open agent terminal"
    # button (e.g. "Powered by Codex", "Powered by Pi Coding"). The harness declares the WHOLE
    # string, prefix included, so a harness that wants no credit at all declares "" -- the
    # frontend then renders nothing. Claude does this today.
    powered_by_text: str
    # Whether this harness can flush a queued "shoulder tap" atomically -- merging the parked
    # messages into the currently-running turn without a restart. True only for codex, whose
    # patched binary watches shoulder_tap_atomic.jsonl and ABA-gates the flush on the live turn
    # id. False harnesses (claude, pi) keep the restart-based flush.
    native_atomic_shoulder_tap_possible: bool = False


class SwitchResult(FrozenModel):
    """The outcome of a :meth:`HarnessModelResolver.switch`. ``ok=False`` carries a
    ``detail`` the endpoint surfaces to the user."""

    ok: bool
    detail: str | None = None


# The one live model-state file every harness writes ({model, effort, fast}), read by
# the shared reader below. The harness's directory for it is per-harness DATA (its
# ``model_state_relative_path`` on the registry); the file NAME is uniform.
MODEL_STATE_NAME: str = "model_state.json"


def model_state_path(state_dir: Path, relative_path: Path) -> Path:
    """The agent's live model-state file: ``<state_dir>/<relative_path>/model_state.json``.

    ``relative_path`` is the harness's registered directory for the file (state-dir root
    for claude/pi, ``plugin/codex/home`` for codex) -- the one per-harness difference the
    shared read/watch path takes as data.
    """
    return state_dir / relative_path / MODEL_STATE_NAME


def read_model_identity(state_path: Path) -> ModelIdentity | None:
    """The live selection from a harness's ``model_state.json``, or None.

    Reads the uniform ``{"model", "effort", "fast"}`` schema every harness writes. Returns
    None when the file is absent, unparseable, or records no model yet (the bar shows
    no slots). Unknown keys are ignored, so an older writer emitting a different effort/tier
    schema still lights the model chip (``model`` is unchanged) with effort None / fast off
    rather than crashing.
    """
    data = read_json_dict(state_path)
    model = data.get("model")
    if not isinstance(model, str) or not model:
        return None
    return ModelIdentity(
        model_id=model,
        effort=parse_effort_level(data.get("effort")),
        fast=data.get("fast") is True,
    )


def match_option(identity: ModelIdentity, options: tuple[ModelOption, ...]) -> ModelOption | None:
    """The catalog option a live ``identity`` resolves to, or None (the shrug case).

    Matches the harness-reported model id (``harness_reported_model_id``, or ``id`` when
    that is None) exactly, then the catalog option id exactly (a provision-time seed
    writes the configured option id, e.g. claude's ``opus[1m]``, before the harness has
    reported anything), then -- for drift tolerance -- by a single prefix pass so a
    dated variant (``claude-haiku-4-5-<date>``) still matches its suffix-free key. Effort
    and fast validity are then checked against the matched option: an effort the option
    does not declare, or fast on a model without fast, is a shrug. Uses the full declared
    effort set, so a live-read hidden level (``ultra``) still matches.
    """
    by_key = {option.harness_reported_model_id or option.id: option for option in options}
    matched = by_key.get(identity.model_id)
    if matched is None:
        matched = next((option for option in options if option.id == identity.model_id), None)
    if matched is None:
        matched = next((option for key, option in by_key.items() if identity.model_id.startswith(key)), None)
    if matched is None:
        return None
    declared = {choice.level for choice in matched.efforts}
    if identity.effort is not None and identity.effort not in declared:
        return None
    if identity.fast and not matched.supports_fast:
        return None
    return matched


def to_options(entries: tuple[tuple[str, tuple[str, ...]], ...]) -> tuple[ModelOption, ...]:
    """Normalize ``(tag, effort_strings)`` pairs into catalog options.

    For the parsed catalogs (pi) that build their options from data rather
    than by hand. The tag is BOTH ``id`` and ``label`` (``provider/model``). Efforts
    are taken verbatim, in the order given (the source's own order). pi has no fast
    tier, so ``supports_fast`` is uniformly False. Duplicate tags
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
    """Applies (for a switchable harness) ONE agent's model choice.

    The live READ is harness-neutral -- every harness writes the uniform
    ``model_state.json`` that :func:`read_model_identity` parses, at the directory
    the harness registers as ``HarnessSpec.model_state_relative_path`` (read-side data on
    the spec, beside ``catalog_factory`` -- deliberately NOT on this resolver, which is
    now write-only) -- so the resolver only owns the harness-specific WRITE side:

    * :meth:`switch` -- apply a selection (a no-op for a display-only harness)
    * :meth:`list_offered_models` -- the picker's offer set (only pi overrides it;
      its offer set is per-agent and auth-gated)

    ``build`` takes the whole :class:`AgentInfo` (like the watcher) so each harness
    reads the paths IT needs and the caller never learns which.
    """

    @classmethod
    @abstractmethod
    def build(cls, agent_info: AgentInfo) -> "HarnessModelResolver":
        """Construct for one agent, not yet reading anything."""

    def list_offered_models(self) -> tuple[str, ...] | None:
        """The model ids to OFFER in the picker right now, or None to offer the whole catalog.

        The catalog is the master list -- every model's label and thinking levels -- but it
        is not the offer set. For a harness whose offerable models are account-gated and
        dynamic (pi: only the providers/models the user is authenticated for), the
        picker calls this per open, so a fresh ``/login`` shows up without a catalog refetch.
        The returned ids are matched back against the catalog for their labels and efforts;
        ids absent from the catalog are simply not shown. The default -- for a small,
        static, non-gated catalog (claude, codex) -- returns None: offer everything.
        """
        return None

    def list_offered_options(self) -> tuple["ModelOption", ...] | None:
        """The FULL per-agent options to render in a DYNAMIC picker, or None for a static catalog.

        A dynamic harness (codex) has no static catalog: its model set, each model's efforts, and
        fast support are all account/daemon-derived, so ids alone (:meth:`list_offered_models`)
        cannot carry the per-model effort/fast the picker needs. Such a harness overrides this to
        return the full :class:`ModelOption`s, fetched fresh per picker-open (D2). The default --
        for a static, catalog-backed harness (claude, pi) -- returns None: the picker renders the
        catalog options (narrowed by :meth:`list_offered_models`)."""
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
        the endpoint to this agent). On failure it returns ``ok=False`` with a
        detail the endpoint surfaces to the user.
        """
