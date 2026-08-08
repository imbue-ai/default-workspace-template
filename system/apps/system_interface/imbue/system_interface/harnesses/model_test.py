"""Unit tests for the shared model spine: the matcher and the per-field merge."""

from imbue.system_interface.harnesses.model import EffortChoice
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.model import base_alias
from imbue.system_interface.harnesses.model import match_option
from imbue.system_interface.harnesses.model import merge_identities
from imbue.system_interface.harnesses.model import to_options

_OPUS = ModelOption(
    id="opus[1m]",
    label="Opus 5 (1M)",
    efforts=(
        EffortChoice(level="medium"),
        EffortChoice(level="ultra", in_picker=False),
    ),
    supports_fast=True,
)
_SONNET = ModelOption(id="sonnet", label="Sonnet 5", efforts=(EffortChoice(level="medium"),), supports_fast=False)
_NO_EFFORT = ModelOption(id="tiny", label="Tiny", efforts=(), supports_fast=False)
_OPTIONS = (_OPUS, _SONNET, _NO_EFFORT)


def test_base_alias_strips_the_context_suffix() -> None:
    assert base_alias("opus[1m]") == "opus"
    assert base_alias("gpt-5.6-sol") == "gpt-5.6-sol"


def test_match_option_matches_by_alias_ignoring_the_suffix() -> None:
    identity = ModelIdentity(model_id="opus", effort="medium", fast=True)
    assert match_option(identity, _OPTIONS) is _OPUS


def test_match_option_accepts_a_declared_but_hidden_effort() -> None:
    # ultra is declared (in_picker=False), so a live read of it still matches.
    identity = ModelIdentity(model_id="opus[1m]", effort="ultra", fast=False)
    assert match_option(identity, _OPTIONS) is _OPUS


def test_match_option_returns_none_for_an_unknown_model() -> None:
    identity = ModelIdentity(model_id="gpt-4", effort="medium", fast=False)
    assert match_option(identity, _OPTIONS) is None


def test_match_option_rejects_fast_on_a_model_without_fast() -> None:
    identity = ModelIdentity(model_id="sonnet", effort="medium", fast=True)
    assert match_option(identity, _OPTIONS) is None


def test_match_option_rejects_an_effort_a_model_does_not_declare() -> None:
    identity = ModelIdentity(model_id="sonnet", effort="high", fast=False)
    assert match_option(identity, _OPTIONS) is None


def test_match_option_matches_a_no_effort_model_only_with_no_effort() -> None:
    assert match_option(ModelIdentity(model_id="tiny", effort=None, fast=False), _OPTIONS) is _NO_EFFORT
    assert match_option(ModelIdentity(model_id="tiny", effort="medium", fast=False), _OPTIONS) is None


def test_merge_identities_returns_the_guess_when_live_is_none() -> None:
    guess = ModelIdentity(model_id="opus[1m]", effort="medium", fast=True)
    assert merge_identities(None, guess) == guess


def test_merge_identities_fills_a_missing_effort_from_the_guess() -> None:
    live = ModelIdentity(model_id="sonnet", effort=None, fast=False)
    guess = ModelIdentity(model_id="opus[1m]", effort="high", fast=True)
    merged = merge_identities(live, guess)
    # model_id and fast come from live; effort falls back to the guess.
    assert merged == ModelIdentity(model_id="sonnet", effort="high", fast=False)


def test_merge_identities_prefers_the_live_effort_when_present() -> None:
    live = ModelIdentity(model_id="sonnet", effort="low", fast=False)
    guess = ModelIdentity(model_id="opus[1m]", effort="high", fast=True)
    merged = merge_identities(live, guess)
    assert merged is not None
    assert merged.effort == "low"


def test_merge_identities_both_none_is_none() -> None:
    # A harness with no launch default and nothing live yet (pi) -> no choice, logo-only.
    assert merge_identities(None, None) is None


def test_merge_identities_live_over_absent_guess() -> None:
    live = ModelIdentity(model_id="anthropic/opus", effort="high", fast=False)
    assert merge_identities(live, None) == live


def test_to_options_tag_is_id_and_label_efforts_verbatim_and_dedup() -> None:
    # The third entry repeats the first tag; a duplicate tag collapses to the first.
    options = to_options(
        (
            ("anthropic/opus", ("off", "low", "high")),
            ("google/gemini", ()),
            ("anthropic/opus", ("low",)),
        )
    )
    assert [o.id for o in options] == ["anthropic/opus", "google/gemini"]
    opus = options[0]
    assert opus.id == opus.label == "anthropic/opus"
    # Efforts are the given strings, in the given order.
    assert [e.level for e in opus.efforts] == ["off", "low", "high"]
    assert opus.supports_fast is False
    # A model with no effort axis.
    assert options[1].efforts == ()
