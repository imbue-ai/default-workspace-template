"""Unit tests for antigravity's read-only model bar."""

import json
from collections.abc import Callable
from pathlib import Path

from imbue.system_interface.harnesses.antigravity.model import ANTIGRAVITY_CATALOG
from imbue.system_interface.harnesses.antigravity.model import AntigravityModelResolver
from imbue.system_interface.harnesses.antigravity.model import derived_option
from imbue.system_interface.harnesses.antigravity.session import AntigravityHarnessSession
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import match_option
from imbue.system_interface.harnesses.session import SessionDeps


def test_the_bar_is_read_only() -> None:
    """agy's `/model` is an interactive TUI picker with no scriptable one-shot form, so the
    bar reflects and never drives. The frontend renders the slots non-interactive off this."""
    assert ANTIGRAVITY_CATALOG.switch_mode is SwitchMode.READ_ONLY


def test_the_harness_credit_is_declared_whole() -> None:
    """The harness declares the ENTIRE credit string, prefix included -- the frontend renders
    it verbatim and adds nothing. It is a pure function of the harness, so it shows even
    while the model bar has no slots to render (before agy's first statusline fire)."""
    assert ANTIGRAVITY_CATALOG.powered_by_text == "Powered by Antigravity"


def test_every_model_is_one_slot() -> None:
    """agy bakes the tier into the model id and has no fast mode, so the bar shows the model
    chip alone -- the shown slots are decided purely by the matched option's data."""
    assert ANTIGRAVITY_CATALOG.options
    for option in ANTIGRAVITY_CATALOG.options:
        assert option.efforts == (), option.id
        assert option.supports_fast is False, option.id


def test_every_catalog_id_matches_itself() -> None:
    """The ids are agy's own, so a live read of any of them resolves to its display name
    rather than the unrecognized shrug."""
    for option in ANTIGRAVITY_CATALOG.options:
        matched = match_option(ModelIdentity(model_id=option.id, effort=None, fast=False), ANTIGRAVITY_CATALOG.options)
        assert matched is not None, option.id
        assert matched.label == option.label


def test_switch_is_refused_rather_than_silently_ignored() -> None:
    resolver = AntigravityModelResolver()
    result = resolver.switch(ModelIdentity(model_id="x", effort=None, fast=False), frozenset(), lambda text: True)
    assert result.ok is False
    assert result.detail


def test_derived_option_reconstructs_a_gemini_style_name() -> None:
    """The staleness fallback: a model newer than the hand-written list still renders a
    readable name. Rebuilt from the id by rule -- see the banner in model.py."""
    assert derived_option("gemini-3.8-flash-high").label == "Gemini 3.8 Flash (High)"
    assert derived_option("gemini-4.0-pro-low").label == "Gemini 4.0 Pro (Low)"
    # No tier suffix -> no parenthesised tier.
    assert derived_option("claude-sonnet-5").label == "Claude Sonnet 5"
    # Never offered as a pick: it is a rendering fallback, not a catalog entry.
    assert derived_option("gemini-3.8-flash-high").in_picker is False


def _session(model_state_path: Path) -> AntigravityHarnessSession:
    unused: Callable[..., object] = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unused"))
    return AntigravityHarnessSession.build(
        SessionDeps(
            harness=HarnessType.ANTIGRAVITY,
            state_dir=model_state_path.parent,
            model_state_path=model_state_path,
            send_to_harness=lambda text: True,
            notify_agents_changed=lambda: None,
            is_tracked=lambda: True,
            on_queue_snapshot=lambda snapshot: None,
            on_user_turn=lambda event: None,
            recompute_activity=lambda: None,
            clear_queue_state=lambda: None,
            catalog_options=lambda: ANTIGRAVITY_CATALOG.options,
            build_interrupter=unused,
            build_shoulder_tap=lambda agent_info: None,
        )
    )


def test_a_known_model_does_not_add_a_derived_option(tmp_path: Path) -> None:
    state = tmp_path / "model_state.json"
    state.write_text(json.dumps({"model": "gemini-3.7-flash-high"}))
    assert _session(state).switch_options() == ANTIGRAVITY_CATALOG.options


def test_an_unknown_model_is_rendered_instead_of_shrugged(tmp_path: Path) -> None:
    """The whole point of the fallback: when Google ships a model newer than the list --
    worst case, a new DEFAULT -- every agy agent would otherwise show the shrug."""
    state = tmp_path / "model_state.json"
    state.write_text(json.dumps({"model": "gemini-9.9-flash-high"}))
    options = _session(state).switch_options()
    assert len(options) == len(ANTIGRAVITY_CATALOG.options) + 1
    identity = ModelIdentity(model_id="gemini-9.9-flash-high", effort=None, fast=False)
    # Without the appended option this is None, which is the shrug.
    assert match_option(identity, ANTIGRAVITY_CATALOG.options) is None
    matched = match_option(identity, options)
    assert matched is not None
    assert matched.label == "Gemini 9.9 Flash (High)"


def test_no_model_state_leaves_the_catalog_alone(tmp_path: Path) -> None:
    """Before agy's first statusline fire there is no file, so the bar renders no slots.

    The "Powered by Antigravity" credit is unaffected -- it is a pure function of the
    harness and deliberately does not vanish when the model bar has nothing to show.
    """
    assert _session(tmp_path / "absent.json").switch_options() == ANTIGRAVITY_CATALOG.options


def test_the_reported_display_name_matches_the_catalog() -> None:
    """agy reports a DISPLAY NAME, not its slug: a live 1.1.19 statusline payload carries
    ``"model":{"id":"Gemini 3.7 Flash (High)",...}``. Matching on that is the whole reason the
    options set ``harness_reported_model_id``; without it every agy model shrugs."""
    identity = ModelIdentity(model_id="Gemini 3.7 Flash (High)", effort=None, fast=False)
    matched = match_option(identity, ANTIGRAVITY_CATALOG.options)
    assert matched is not None
    assert matched.id == "gemini-3.7-flash-high"


def test_an_effort_would_shrug_which_is_why_none_is_written() -> None:
    """The payload also carries ``"effort":"high"``, and the statusline deliberately does not
    write it: agy's options declare no efforts, and match_option rejects an effort its matched
    option does not declare. Pinned so nobody "helpfully" starts writing it."""
    identity = ModelIdentity(model_id="Gemini 3.7 Flash (High)", effort="high", fast=False)
    assert match_option(identity, ANTIGRAVITY_CATALOG.options) is None


def test_an_unknown_display_name_is_used_verbatim() -> None:
    """A model newer than the list is already human-readable as reported, so it needs no
    reconstruction -- unlike a slug."""
    assert derived_option("Gemini 4.2 Ultra (High)").label == "Gemini 4.2 Ultra (High)"
