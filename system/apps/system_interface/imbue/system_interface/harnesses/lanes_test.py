"""Tests for the lane table.

Most of these guard the table itself rather than logic: it is a hand-written pile of
regexes, argv and keystrokes, and a typo in any of them fails at sign-in time against a real
CLI -- the slowest possible place to find out.
"""

import re

import pytest

from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.lanes import HARNESS_LABEL
from imbue.system_interface.harnesses.lanes import LANES
from imbue.system_interface.harnesses.lanes import LANE_ANTHROPIC
from imbue.system_interface.harnesses.lanes import LANE_API_KEY
from imbue.system_interface.harnesses.lanes import LANE_GOOGLE
from imbue.system_interface.harnesses.lanes import LANE_OPENAI
from imbue.system_interface.harnesses.lanes import PasteMethod
from imbue.system_interface.harnesses.lanes import PtyMethod
from imbue.system_interface.harnesses.lanes import Submit
from imbue.system_interface.harnesses.lanes import account_label
from imbue.system_interface.harnesses.lanes import get_lane
from imbue.system_interface.harnesses.lanes import get_method


def test_lane_ids_are_unique() -> None:
    ids = [lane.id for lane in LANES]
    assert len(ids) == len(set(ids))


def test_every_lane_has_a_primary_method_and_unique_method_ids() -> None:
    for lane in LANES:
        assert lane.methods, f"{lane.id} has no methods"
        method_ids = [m.id for m in lane.methods]
        assert len(method_ids) == len(set(method_ids)), f"{lane.id} repeats a method id"


def test_every_harness_a_lane_names_has_a_label() -> None:
    """The label is the "(Claude Code)" half of every account row; a missing one would
    render an account nobody can attribute."""
    for lane in LANES:
        assert lane.harness in HARNESS_LABEL


def test_every_pattern_in_the_table_compiles() -> None:
    for lane in LANES:
        for method in lane.methods:
            if not isinstance(method, PtyMethod):
                continue
            for pattern in (
                method.scrape.trigger,
                method.scrape.strict,
                method.scrape.continuation,
                method.expect_before_keys,
                method.success,
                *(p for p, _ in method.failures),
            ):
                if pattern is not None:
                    re.compile(pattern)


def test_a_scrape_trigger_matches_what_its_strict_pattern_matches() -> None:
    """The trigger only wakes `expect`; if it can fire on text the strict pattern would
    never claim, the flow wakes up and then finds nothing to extract."""
    samples = {
        "anthropic": "https://claude.ai/oauth/authorize?code=1",
        "google": "https://accounts.google.com/o/oauth2/auth?client_id=x",
        "openai": "ED1D-9U4FY",
    }
    for lane_id, sample in samples.items():
        method = get_lane(lane_id).methods[0]
        assert isinstance(method, PtyMethod)
        assert re.search(method.scrape.trigger, sample), lane_id
        assert re.search(method.scrape.strict, sample), lane_id


def test_codex_scrapes_a_code_against_a_fixed_url_and_submits_nothing() -> None:
    """codex inverts the usual shape, and the rest of the flow branches on exactly this."""
    method = get_method("openai", "device")
    assert isinstance(method, PtyMethod)
    assert method.static_url == "https://auth.openai.com/codex/device"
    assert method.submit is Submit.NONE
    # Its success signal is the process exiting, not a line on screen.
    assert method.success is None


def test_agy_methods_assert_the_menu_before_typing() -> None:
    """agy's keystrokes are a blind script: without a screen assertion, a reordered menu
    silently selects a different login method and nothing fails."""
    for method in LANE_GOOGLE.methods:
        assert isinstance(method, PtyMethod)
        assert method.expect_before_keys, method.id
        assert method.keys


def test_agy_declares_no_frame_marker() -> None:
    """It renders without Ink's synchronized updates, so the replay sees only the final
    screen. Recorded here so a future frame-marker default cannot silently apply to it."""
    for method in LANE_GOOGLE.methods:
        assert isinstance(method, PtyMethod)
        assert method.frame_marker is None


def test_setup_token_is_the_one_method_whose_output_is_the_credential() -> None:
    method = get_method("anthropic", "setup_token")
    assert isinstance(method, PtyMethod)
    assert method.result_scrape is not None
    assert method.result_sink is not None
    # It completes on the CLI's own polling as well as on a pasted code.
    assert method.submit is Submit.OPTIONAL


def test_both_pi_lanes_are_paste_only() -> None:
    """No terminal is involved on pi, which is why neither lane needs a promote probe."""
    for lane in (get_lane("opencode-go"), LANE_API_KEY):
        assert lane.harness is HarnessType.PI_CODING
        for method in lane.methods:
            assert isinstance(method, PasteMethod)


def test_only_the_key_lanes_offer_key_providers() -> None:
    assert LANE_API_KEY.key_providers
    assert get_lane("opencode-go").key_providers
    assert LANE_ANTHROPIC.key_providers == ()
    assert LANE_OPENAI.key_providers == ()


def test_key_provider_ids_are_unique_within_a_lane() -> None:
    for lane in LANES:
        ids = [k.provider_id for k in lane.key_providers]
        assert len(ids) == len(set(ids)), lane.id


def test_account_label_numbers_from_the_second() -> None:
    assert account_label("Anthropic", HarnessType.CLAUDE, 1) == "Anthropic (Claude Code)"
    assert account_label("Anthropic", HarnessType.CLAUDE, 2) == "Anthropic (Claude Code) 2"
    # The key lane's display noun is the provider, not the words "API key".
    assert account_label("OpenRouter", HarnessType.PI_CODING, 1) == "OpenRouter (Pi)"


def test_lookups_raise_on_unknown_ids() -> None:
    with pytest.raises(KeyError):
        get_lane("nope")
    with pytest.raises(KeyError):
        get_method("anthropic", "nope")
