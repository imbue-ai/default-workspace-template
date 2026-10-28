"""Tests for the lane table.

Most of these guard the table itself rather than logic: it is a hand-written pile of
regexes, argv and keystrokes, and a typo in any of them fails at sign-in time against a real
CLI -- the slowest possible place to find out.
"""

import re

import pytest

from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.lanes import AppServerMethod
from imbue.chat.harnesses.lanes import CodexLogin
from imbue.chat.harnesses.lanes import HARNESS_LABEL
from imbue.chat.harnesses.lanes import LANES
from imbue.chat.harnesses.lanes import LANE_ANTHROPIC
from imbue.chat.harnesses.lanes import LANE_API_KEY
from imbue.chat.harnesses.lanes import LANE_GOOGLE
from imbue.chat.harnesses.lanes import LANE_OPENAI
from imbue.chat.harnesses.lanes import LANE_XAI
from imbue.chat.harnesses.lanes import LaneNotFoundError
from imbue.chat.harnesses.lanes import PasteMethod
from imbue.chat.harnesses.lanes import PasteSink
from imbue.chat.harnesses.lanes import PtyMethod
from imbue.chat.harnesses.lanes import account_label
from imbue.chat.harnesses.lanes import get_lane
from imbue.chat.harnesses.lanes import get_method
from imbue.chat.harnesses.lanes import numbered_provider


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
            code_scrape = method.device_code_scrape
            for pattern in (
                method.scrape.trigger,
                method.scrape.strict,
                method.scrape.continuation,
                *(() if code_scrape is None else (code_scrape.trigger, code_scrape.strict, code_scrape.continuation)),
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
        "xai": "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
    }
    for lane_id, sample in samples.items():
        method = get_lane(lane_id).methods[0]
        assert isinstance(method, PtyMethod)
        assert re.search(method.scrape.trigger, sample), lane_id
        assert re.search(method.scrape.strict, sample), lane_id


@pytest.mark.parametrize(
    ("line", "page"),
    [
        pytest.param(
            "  https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
            "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
            id="accounts-host",
        ),
        pytest.param(
            "  https://auth.x.ai/oauth2/device?user_code=ABCD-1234",
            "https://auth.x.ai/oauth2/device?user_code=ABCD-1234",
            id="issuer-host",
        ),
        pytest.param("  https://x.ai/legal/terms-of-service", None, id="another-x-ai-link"),
        pytest.param("  https://accounts.x.ai.example.test/oauth2/device", None, id="a-host-outside-x-ai"),
    ],
)
def test_grok_s_page_scrape_takes_the_device_page_on_any_x_ai_host(line: str, page: str | None) -> None:
    method = LANE_XAI.methods[0]
    assert isinstance(method, PtyMethod)
    found = re.search(method.scrape.strict, line.ljust(80))
    assert (None if found is None else found.group().rstrip()) == page


def test_chatgpt_signs_in_through_codexs_app_server_in_a_browser_first() -> None:
    """The browser login needs the desktop app's relay; the device login is the fallback without one."""
    browser, device, api_key = LANE_OPENAI.methods
    assert isinstance(browser, AppServerMethod)
    assert (browser.id, browser.login) == ("chatgpt", CodexLogin.BROWSER)
    assert browser.label == "Use your ChatGPT plan (runs on Codex)"
    assert isinstance(device, AppServerMethod)
    assert (device.id, device.login) == ("device", CodexLogin.DEVICE)
    assert isinstance(api_key, PasteMethod)
    assert api_key.sink is PasteSink.CODEX_AUTH_JSON


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


def test_xai_signs_in_with_grok_s_device_code_first_and_a_key_second() -> None:
    """grok's device sign-in prints a code to confirm in the browser and decides itself, by its exit
    and its success line, so it needs no relay and no frames to replay."""
    device, api_key = LANE_XAI.methods
    assert LANE_XAI.harness is HarnessType.GROK
    assert isinstance(device, PtyMethod)
    assert device.argv == ("login", "--device-auth")
    assert device.device_code_scrape is not None
    assert device.success is not None
    assert device.frame_marker is None
    assert not device.relays_browser_sign_in
    assert isinstance(api_key, PasteMethod)
    assert (api_key.id, api_key.sink, api_key.signup_url, api_key.key_hint) == (
        "api_key",
        PasteSink.GROK_AUTH_JSON,
        "https://console.x.ai",
        "xai-...",
    )


@pytest.mark.parametrize(
    ("line", "is_code"),
    [
        pytest.param("  ABCD-1234", True, id="the-code-row"),
        pytest.param("  K7QX2M9P", True, id="a-code-without-a-dash"),
        pytest.param("  https://accounts.x.ai/oauth2/device?user_code=ABCD-1234", False, id="the-url-row"),
        pytest.param("  (Could not open browser automatically", False, id="the-no-browser-row"),
        pytest.param("Waiting for authorization...", False, id="an-unindented-row"),
    ],
)
def test_grok_s_code_scrape_claims_only_the_row_that_is_the_code(line: str, is_code: bool) -> None:
    """The URL row carries the same code and the same indent, so a looser pattern would read it as the code."""
    method = LANE_XAI.methods[0]
    assert isinstance(method, PtyMethod) and method.device_code_scrape is not None
    # Rows reach the pattern padded to the screen width.
    assert (re.search(method.device_code_scrape.strict, line.ljust(80)) is not None) is is_code


def test_setup_token_is_the_one_method_whose_output_is_the_credential() -> None:
    method = get_method("anthropic", "setup_token")
    assert isinstance(method, PtyMethod)
    assert method.result_scrape is not None
    assert method.result_sink is not None


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
    assert LANE_XAI.key_providers == ()


def test_key_provider_ids_are_unique_within_a_lane() -> None:
    for lane in LANES:
        ids = [k.provider_id for k in lane.key_providers]
        assert len(ids) == len(set(ids)), lane.id


def test_account_label_numbers_from_the_second() -> None:
    assert account_label("Anthropic", HarnessType.CLAUDE, 1) == "Anthropic (Claude Code)"
    assert account_label("Anthropic", HarnessType.CLAUDE, 2) == "Anthropic 2 (Claude Code)"
    # The key lane's display noun is the provider, not the words "API key".
    assert account_label("OpenRouter", HarnessType.PI_CODING, 1) == "OpenRouter (Pi)"


def test_the_number_sits_on_the_provider_noun() -> None:
    """It has to: the row draws the provider and the harness as two separate spans, so a
    number trailing the composed string has nowhere to be drawn."""
    assert numbered_provider("Anthropic", 1) == "Anthropic"
    assert numbered_provider("Anthropic", 2) == "Anthropic 2"


def test_lookups_raise_on_unknown_ids() -> None:
    with pytest.raises(LaneNotFoundError):
        get_lane("nope")
    with pytest.raises(LaneNotFoundError):
        get_method("anthropic", "nope")
