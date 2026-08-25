"""Unit tests for the shared model spine: the live-state reader and the matcher."""

import json
from pathlib import Path

from imbue.system_interface.harnesses.model import EffortChoice
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.model import match_option
from imbue.system_interface.harnesses.model import model_state_path
from imbue.system_interface.harnesses.model import read_model_identity
from imbue.system_interface.harnesses.model import resolve_model_choice
from imbue.system_interface.harnesses.model import to_options

# opus reports a suffix-free API id that differs from its switch id (the [1m] alias).
_OPUS = ModelOption(
    id="opus[1m]",
    label="Opus 5 (1M)",
    efforts=(
        EffortChoice(level="medium"),
        EffortChoice(level="ultra", in_picker=False),
    ),
    supports_fast=True,
    harness_reported_model_id="claude-opus-4-8",
)
# haiku's reported id is suffix-free; a dated variant must still match via the prefix pass.
_HAIKU = ModelOption(
    id="haiku",
    label="Haiku 4.5",
    efforts=(EffortChoice(level="medium"),),
    supports_fast=False,
    harness_reported_model_id="claude-haiku-4-5",
)
# codex/pi leave harness_reported_model_id None: the reported id equals the switch id.
_GPT = ModelOption(id="gpt-5.6-sol", label="GPT-5.6-Sol", efforts=(EffortChoice(level="high"),), supports_fast=True)
_NO_EFFORT = ModelOption(id="tiny", label="Tiny", efforts=(), supports_fast=False)
_OPTIONS = (_OPUS, _HAIKU, _GPT, _NO_EFFORT)


# --- reader ---------------------------------------------------------------------


def test_model_state_path_joins_relative_dir(tmp_path: Path) -> None:
    assert model_state_path(tmp_path, Path(".")) == tmp_path / "model_state.json"
    assert model_state_path(tmp_path, Path("plugin/codex/home")) == tmp_path / "plugin/codex/home/model_state.json"


def test_read_model_identity_reads_model_effort_and_fast(tmp_path: Path) -> None:
    path = tmp_path / "model_state.json"
    path.write_text(json.dumps({"model": "claude-fable-5", "effort": "xhigh", "fast": False}))
    assert read_model_identity(path) == ModelIdentity(model_id="claude-fable-5", effort="xhigh", fast=False)


def test_read_model_identity_fast_true(tmp_path: Path) -> None:
    path = tmp_path / "model_state.json"
    path.write_text(json.dumps({"model": "claude-opus-4-8", "effort": "max", "fast": True}))
    assert read_model_identity(path) == ModelIdentity(model_id="claude-opus-4-8", effort="max", fast=True)


def test_read_model_identity_none_when_file_absent(tmp_path: Path) -> None:
    # Missing file -> None -> the bar shows logo-only.
    assert read_model_identity(tmp_path / "nope.json") is None


def test_read_model_identity_none_when_no_model(tmp_path: Path) -> None:
    path = tmp_path / "model_state.json"
    path.write_text(json.dumps({"effort": "high", "fast": True}))
    assert read_model_identity(path) is None


def test_read_model_identity_effort_none_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "model_state.json"
    path.write_text(json.dumps({"model": "claude-fable-5", "fast": False}))
    live = read_model_identity(path)
    assert live is not None
    assert live.effort is None


def test_read_model_identity_tolerates_the_old_codex_schema(tmp_path: Path) -> None:
    # An installed codex binary that still writes {model, reasoning_effort, service_tier}
    # must NOT crash: the model chip lights (model is unchanged), effort is None (unknown
    # key ignored), and fast is off (no `fast` key).
    path = tmp_path / "model_state.json"
    path.write_text(json.dumps({"model": "gpt-5.6-sol", "reasoning_effort": "high", "service_tier": "priority"}))
    assert read_model_identity(path) == ModelIdentity(model_id="gpt-5.6-sol", effort=None, fast=False)


# --- matcher --------------------------------------------------------------------


def test_match_option_matches_the_reported_id_exactly() -> None:
    identity = ModelIdentity(model_id="claude-opus-4-8", effort="medium", fast=True)
    assert match_option(identity, _OPTIONS) is _OPUS


def test_match_option_falls_back_to_id_when_reported_id_is_none() -> None:
    # codex/pi options leave harness_reported_model_id None, so the switch id is the key.
    identity = ModelIdentity(model_id="gpt-5.6-sol", effort="high", fast=False)
    assert match_option(identity, _OPTIONS) is _GPT


def test_match_option_matches_the_catalog_option_id() -> None:
    # A provision-time seed writes the configured option id (claude's settings-style
    # "opus[1m]") before the harness has reported anything; it must resolve to the same
    # option the later harness-reported id does.
    identity = ModelIdentity(model_id="opus[1m]", effort="medium", fast=False)
    assert match_option(identity, _OPTIONS) is _OPUS


def test_match_option_prefix_matches_a_dated_variant() -> None:
    # A dated id keeps matching the suffix-free reported key via the single prefix pass.
    identity = ModelIdentity(model_id="claude-haiku-4-5-20251001", effort="medium", fast=False)
    assert match_option(identity, _OPTIONS) is _HAIKU


def test_match_option_accepts_a_declared_but_hidden_effort() -> None:
    # ultra is declared (in_picker=False), so a live read of it still matches.
    identity = ModelIdentity(model_id="claude-opus-4-8", effort="ultra", fast=False)
    assert match_option(identity, _OPTIONS) is _OPUS


def test_match_option_returns_none_for_an_unknown_model() -> None:
    identity = ModelIdentity(model_id="gpt-4", effort="medium", fast=False)
    assert match_option(identity, _OPTIONS) is None


def test_fast_on_a_model_without_fast_drops_the_flag_instead_of_shrugging() -> None:
    """A stale fast flag must not hide a model the catalog knows.

    Fast is recorded in launch settings and so outlives the model it was chosen for, which is
    re-read from the session. Any path that changes one without the other (a restart landing on
    a different model, a model switch) produces this pairing, and it used to blank the bar.
    """
    identity = ModelIdentity(model_id="claude-haiku-4-5", effort="medium", fast=True)
    assert match_option(identity, _OPTIONS) is _HAIKU

    choice = resolve_model_choice(identity, _OPTIONS)
    assert choice.matched is _HAIKU
    assert choice.identity.fast is False, "the flag is dropped, not carried through to the bar"


def test_fast_is_preserved_on_a_model_that_supports_it() -> None:
    identity = ModelIdentity(model_id="claude-opus-4-8", effort="medium", fast=True)
    choice = resolve_model_choice(identity, _OPTIONS)
    assert choice.matched is _OPUS
    assert choice.identity.fast is True


def test_an_unmatched_identity_is_still_the_shrug() -> None:
    """Dropping fast is not the same as matching anything: an unknown model still shrugs."""
    choice = resolve_model_choice(ModelIdentity(model_id="who-knows", effort=None, fast=True), _OPTIONS)
    assert choice.matched is None
    assert choice.identity.fast is True, "nothing was matched, so there is no claim to correct"


def test_match_option_rejects_an_effort_a_model_does_not_declare() -> None:
    identity = ModelIdentity(model_id="claude-haiku-4-5", effort="high", fast=False)
    assert match_option(identity, _OPTIONS) is None


def test_match_option_matches_a_no_effort_model_only_with_no_effort() -> None:
    assert match_option(ModelIdentity(model_id="tiny", effort=None, fast=False), _OPTIONS) is _NO_EFFORT
    assert match_option(ModelIdentity(model_id="tiny", effort="medium", fast=False), _OPTIONS) is None


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
    # to_options leaves harness_reported_model_id at its default (None -> reported id == id).
    assert opus.harness_reported_model_id is None
    # A model with no effort axis.
    assert options[1].efforts == ()
