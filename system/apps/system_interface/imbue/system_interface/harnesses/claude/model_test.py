"""Unit tests for the Claude model resolver's switch side (the live read is shared)."""

from pathlib import Path

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.claude.model import _CLAUDE_EFFORTS
from imbue.system_interface.harnesses.claude.model import CLAUDE_CATALOG
from imbue.system_interface.harnesses.claude.model import ClaudeModelResolver
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import match_option


def _agent_info(tmp_path: Path) -> AgentInfo:
    config_dir = tmp_path / "claude_config"
    config_dir.mkdir()
    (tmp_path / "state").mkdir()
    return AgentInfo(
        id="agent-1",
        name="a",
        state="RUNNING",
        agent_state_dir=tmp_path / "state",
        claude_config_dir=config_dir,
    )


def test_offered_options_carry_suffix_free_reported_ids() -> None:
    # The matcher keys off harness_reported_model_id; the statusline reports suffix-free
    # API ids, so an offered option's switch alias and its reported id are NOT the same
    # string. This is the mapping the picker promises.
    reported = {
        option.id: option.harness_reported_model_id
        for option in CLAUDE_CATALOG.options
        if option.in_picker
    }
    assert reported == {
        "fable[1m]": "claude-fable-5",
        "opus[1m]": "claude-opus-5",
        "sonnet[1m]": "claude-sonnet-5",
        "haiku": "claude-haiku-4-5",
    }


def test_picker_offers_exactly_four_models() -> None:
    # Fable 5, Opus 5, Sonnet 5, Haiku 4.5 -- in the order claude 2.1.227's own /model
    # picker ranks them. Everything else in the catalog is display-only.
    offered = [(option.id, option.label) for option in CLAUDE_CATALOG.options if option.in_picker]
    assert offered == [
        ("fable[1m]", "Fable 5"),
        ("opus[1m]", "Opus 5"),
        ("sonnet[1m]", "Sonnet 5"),
        ("haiku", "Haiku 4.5"),
    ]


def test_hidden_options_are_the_models_the_picker_cannot_reach() -> None:
    # The hidden set is defined by what the four offered models do NOT match: an agent
    # sitting on one of these (an approved org on Mythos, or a user who typed /model
    # opus-4-8 into the underlying session) still shows a name instead of shrugging.
    hidden = [option.id for option in CLAUDE_CATALOG.options if not option.in_picker]
    assert hidden == [
        "claude-mythos-5",
        "claude-mythos-preview",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-opus-4-1",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-sonnet-4.6",
        # Family catch-alls, last so the prefix pass cannot let them swallow the above.
        "claude-opus-4",
        "claude-sonnet-4",
        "claude-haiku-4",
    ]
    # Each of them actually resolves, rather than merely being declared.
    for model_id in hidden:
        matched = match_option(
            ModelIdentity(model_id=model_id, effort="high", fast=False), CLAUDE_CATALOG.options
        )
        assert matched is not None, f"hidden option {model_id} does not match itself"
        assert matched.id == model_id


def test_no_catalog_key_shadows_another_in_the_prefix_pass() -> None:
    # match_option's prefix pass walks the options in catalog order and takes the FIRST
    # key the reported id starts with, so a key that prefixes a later one would silently
    # swallow it -- a bare "claude-opus-4" would capture every dated Opus 4.x. Adding one
    # is the trap this guards; the fix is to spell the family out (claude-opus-4-8).
    keys = [option.harness_reported_model_id or option.id for option in CLAUDE_CATALOG.options]
    for index, key in enumerate(keys):
        for other in keys[index + 1 :]:
            assert not other.startswith(key), f"{key} shadows {other} in the prefix pass"


def test_fast_mode_follows_the_binary_not_model_rank() -> None:
    # Claude 2.1.227 scopes fast mode to "Opus 5/4.8": 4.7 and 4.6 had it removed, and
    # Fable does not have it at all despite outranking Opus in capability. supports_fast
    # also gates matching -- an agent on Opus 4.8 with fast on shrugs without the flag --
    # so this is not a cosmetic field on the hidden entries.
    fast = [option.id for option in CLAUDE_CATALOG.options if option.supports_fast]
    assert fast == ["opus[1m]", "claude-opus-4-8"]


def test_every_option_declares_the_full_effort_set() -> None:
    # Efforts are the one field the hidden entries do not take from the binary. Per-model
    # effort support is not stated plainly anywhere (Opus 4.5 takes low/medium/high,
    # Sonnet 4.5 rejects the axis), and match_option shrugs on a level the option does not
    # declare -- so guessing narrow risks a shrug and buys nothing, since a hidden option's
    # effort set is never shown in a picker. supports_fast deliberately goes the other way.
    declared = {choice.level for choice in _CLAUDE_EFFORTS}
    for option in CLAUDE_CATALOG.options:
        assert {choice.level for choice in option.efforts} == declared


# Every claude model id the pinned 2.1.227 binary carries, extracted from its strings
# rather than transcribed from docs:
#
#     strings -n 8 claude | grep -oE "claude-(opus|sonnet|haiku|fable|mythos)[a-z0-9._-]*(\\[[12]m\\])?"
#
# Truncation fragments ("claude-opus-") and the news-URL slug (claude-fable-5-mythos-5)
# are dropped; everything else is a real id the statusline could report. Regenerate this
# list against the binary whenever CLAUDE_CODE_VERSION moves.
_BINARY_MODEL_IDS: tuple[str, ...] = (
    "claude-fable-5",
    "claude-haiku-4",
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "claude-haiku-4-5-20251001-v1",
    "claude-mythos-5",
    "claude-mythos-preview",
    "claude-opus-4",
    "claude-opus-4-0",
    "claude-opus-4-1",
    "claude-opus-4-1-20250805",
    "claude-opus-4-1-20250805-v1",
    "claude-opus-4-20250514",
    "claude-opus-4-20250514-v1",
    "claude-opus-4-5",
    "claude-opus-4-5-20251101",
    "claude-opus-4-5-20251101-v1",
    "claude-opus-4-6",
    "claude-opus-4-6-20251101",
    "claude-opus-4-6-fast",
    "claude-opus-4-6-v1",
    "claude-opus-4-6[1m]",
    "claude-opus-4-7",
    "claude-opus-4-7-fast",
    "claude-opus-4-7[1m]",
    "claude-opus-4-8",
    "claude-opus-4-8[1m]",
    "claude-opus-5",
    "claude-opus-5[1m]",
    "claude-sonnet-4",
    "claude-sonnet-4-0",
    "claude-sonnet-4-20250514",
    "claude-sonnet-4-20250514-v1",
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-5-20250929-v1",
    "claude-sonnet-4-5-20250929[1m]",
    "claude-sonnet-4-6",
    "claude-sonnet-4-6-20251114",
    "claude-sonnet-4-6[1m]",
    "claude-sonnet-4.6",
    "claude-sonnet-5",
)


def test_every_binary_model_id_resolves() -> None:
    # The completeness guarantee: no id the harness can report falls through to the shrug
    # case. Checked with fast off -- fast is a separate axis and only a model that declares
    # supports_fast may arrive with it on.
    unresolved = [
        model_id
        for model_id in _BINARY_MODEL_IDS
        if match_option(
            ModelIdentity(model_id=model_id, effort="high", fast=False), CLAUDE_CATALOG.options
        )
        is None
    ]
    assert unresolved == [], f"these reported ids would shrug: {unresolved}"


def test_binary_model_ids_resolve_to_their_own_family() -> None:
    # Coverage alone is not enough: the prefix pass could resolve every id to the WRONG
    # option and still report zero shrugs. Pin the family instead of each exact label, so
    # adding a point release does not churn this test.
    for model_id in _BINARY_MODEL_IDS:
        matched = match_option(
            ModelIdentity(model_id=model_id, effort="high", fast=False), CLAUDE_CATALOG.options
        )
        assert matched is not None
        family = matched.label.split()[0].lower()
        assert family in model_id, f"{model_id} resolved to {matched.label}, a different family"


def test_live_statusline_model_ids_match_their_catalog_option() -> None:
    # The model ids claude 2.1.227's statusline actually reports, captured from a live
    # binary launched exactly as the workspace launches it (settings.json model="opus[1m]",
    # then /model sonnet, /model haiku). None of them is a bare catalog key any more: opus
    # and sonnet keep their [1m] launch suffix and haiku reports a dated id, so all three
    # reach their option through match_option's prefix pass rather than an exact key hit.
    # claude-sonnet-5[1m] is the one id here NOT captured live -- it is what the sonnet[1m]
    # switch must report given the [1m] suffix survives into opus's reported id, and is
    # pinned so the prefix pass is exercised for it too.
    for reported_id, expected_label in (
        ("claude-fable-5", "Fable 5"),
        ("claude-fable-5[1m]", "Fable 5"),
        ("claude-opus-5[1m]", "Opus 5"),
        ("claude-sonnet-5", "Sonnet 5"),
        ("claude-sonnet-5[1m]", "Sonnet 5"),
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
        # A dated legacy id reaches its hidden option the same way.
        ("claude-opus-4-5-20251101", "Opus 4.5"),
    ):
        matched = match_option(
            ModelIdentity(model_id=reported_id, effort="high", fast=False), CLAUDE_CATALOG.options
        )
        assert matched is not None, f"the live statusline id {reported_id} matches no catalog option"
        assert matched.label == expected_label


def test_switch_sends_only_the_axes_it_is_told(tmp_path: Path) -> None:
    # Told model + effort changed but not fast: /fast must NOT be sent.
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []

    def send(line: str) -> bool:
        sent.append(line)
        return True

    result = resolver.switch(
        ModelIdentity(model_id="sonnet[1m]", effort="high", fast=False),
        frozenset({ModelAxis.MODEL, ModelAxis.EFFORT}),
        send,
    )

    assert result.ok
    assert sent == ["/model sonnet[1m]", "/effort high"]


def test_switch_fast_toggle_sends_only_fast(tmp_path: Path) -> None:
    # Only the fast axis changed: send just /fast on, not /model or /effort.
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="opus[1m]", effort="medium", fast=True),
        frozenset({ModelAxis.FAST}),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == ["/fast on"]


def test_switch_reissues_a_value_the_agent_is_already_on(tmp_path: Path) -> None:
    # The reported bug: the effort axis is in the change set (the user went medium ->
    # xhigh -> medium faster than the state file reconciled). The switch must still send
    # /effort medium -- it applies the axes it is told, never suppressing on a disk read.
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="opus[1m]", effort="medium", fast=False),
        frozenset({ModelAxis.EFFORT}),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == ["/effort medium"]


def test_switch_with_no_axes_sends_nothing(tmp_path: Path) -> None:
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="sonnet", effort="high", fast=False),
        frozenset(),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == []


def test_switch_reports_a_failed_send(tmp_path: Path) -> None:
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    result = resolver.switch(
        ModelIdentity(model_id="sonnet", effort="high", fast=False),
        frozenset({ModelAxis.MODEL}),
        lambda _line: False,
    )
    assert not result.ok
    assert result.detail is not None


def test_switch_records_fast_off_durably(tmp_path: Path) -> None:
    # Claude Code leaves no durable record of fast-off, so a fast switch writes fastMode
    # into the agent's launch settings (what a restart comes back with).
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    result = resolver.switch(
        ModelIdentity(model_id="opus[1m]", effort="medium", fast=False),
        frozenset({ModelAxis.FAST}),
        lambda _line: True,
    )
    assert result.ok
