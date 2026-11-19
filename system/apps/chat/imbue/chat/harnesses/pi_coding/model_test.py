"""Unit tests for pi's parsed catalog, its model resolver, and the locked flush writer."""

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.claude.model import ClaudeModelResolver
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.model import ModelAxis
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import PickerMode
from imbue.chat.harnesses.model import SwitchMode
from imbue.chat.harnesses.pi_coding.local_model import LocalModelError
from imbue.chat.harnesses.pi_coding.local_model_account import LocalModelAccount
from imbue.chat.harnesses.pi_coding.local_model_account import LocalModelRecord
from imbue.chat.harnesses.pi_coding.local_model_account import write_account_models
from imbue.chat.harnesses.pi_coding.model import PiFlushTapStatus
from imbue.chat.harnesses.pi_coding.model import PiModelResolver
from imbue.chat.harnesses.pi_coding.model import _parse_list_models
from imbue.chat.harnesses.pi_coding.model import _supported_thinking_levels
from imbue.chat.harnesses.pi_coding.model import build_catalog
from imbue.chat.harnesses.pi_coding.model import flush_pi_queue_atomic
from imbue.chat.harnesses.pi_coding.model import read_custom_model_options
from imbue.chat.harnesses.registry import get_catalog
from imbue.chat.testing import put_stand_in_pi_on_path


def _agent_info(tmp_path: Path) -> AgentInfo:
    return AgentInfo(
        id="pi-1",
        name="a",
        state="RUNNING",
        agent_state_dir=tmp_path,
        claude_config_dir=tmp_path / "unused",
        harness=HarnessType.PI_CODING,
    )


# effort derivation (pi's getSupportedThinkingLevels)


def test_non_reasoning_model_supports_only_off() -> None:
    # The reasoning gate short-circuits: a non-reasoning model offers only "off",
    # whatever its thinkingLevelMap says.
    assert _supported_thinking_levels({"reasoning": False, "thinkingLevelMap": {"high": "high"}}) == ("off",)


def test_reasoning_model_sparse_map_nulls_disable_and_xhigh_max_opt_in() -> None:
    # A nulled level is dropped; xhigh/max appear only when explicitly mapped; every
    # other level is on. (This mirrors a real anthropic entry: off nulled, xhigh/max mapped.)
    levels = _supported_thinking_levels(
        {"reasoning": True, "thinkingLevelMap": {"off": None, "xhigh": "xhigh", "max": "max"}}
    )
    assert levels == ("minimal", "low", "medium", "high", "xhigh", "max")


def test_reasoning_model_no_map_drops_xhigh_and_max() -> None:
    # With no map, everything up to high is on, but xhigh/max are opt-in only.
    assert _supported_thinking_levels({"reasoning": True}) == ("off", "minimal", "low", "medium", "high")


# catalog build (efforts are verbatim strings)


def _write_provider(data_dir: Path, provider: str, models: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / f"{provider}.json").write_text(json.dumps({f"{provider}-api": models}))


def test_build_catalog_tags_and_string_efforts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_provider(
        data_dir,
        "anthropic",
        {
            "claude-sonnet-5": {"reasoning": True, "thinkingLevelMap": {"off": None, "xhigh": "xhigh"}},
            "haiku-lite": {"reasoning": False},
        },
    )
    catalog = build_catalog(data_dir)
    assert catalog.picker_mode == PickerMode.SEARCH
    assert catalog.switch_mode == SwitchMode.EAGER_THEN_RECONCILE
    by_id = {opt.id: opt for opt in catalog.options}
    # id == label == provider/model
    assert by_id["anthropic/claude-sonnet-5"].label == "anthropic/claude-sonnet-5"
    # efforts are the model's thinking levels, verbatim strings, in pi's order
    assert [e.level for e in by_id["anthropic/claude-sonnet-5"].efforts] == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    # a non-reasoning model still declares its single "off" level
    assert [e.level for e in by_id["anthropic/haiku-lite"].efforts] == ["off"]
    # no model supports fast
    assert all(not opt.supports_fast for opt in catalog.options)


def test_the_pi_catalog_is_read_from_the_pi_on_path_even_after_another_pi_was_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_stand_in_pi_on_path(tmp_path / "first", "openrouter", ["vendor/first-model"], monkeypatch)
    assert [option.id for option in get_catalog(HarnessType.PI_CODING).options] == ["openrouter/vendor/first-model"]

    put_stand_in_pi_on_path(tmp_path / "second", "groq", ["vendor/second-model"], monkeypatch)
    assert [option.id for option in get_catalog(HarnessType.PI_CODING).options] == ["groq/vendor/second-model"]


# pi --list-models parsing (the authed offer set)


def test_parse_list_models_takes_provider_and_model_columns() -> None:
    # Real `pi --list-models` output: a header row then whitespace-column rows whose
    # first two columns are provider and model.
    output = (
        "provider   model                     context  max-out  thinking  images\n"
        "anthropic  claude-opus-4-8           1M       128K     yes       yes\n"
        "anthropic  claude-sonnet-5           1M       128K     yes       yes\n"
    )
    assert _parse_list_models(output) == ("anthropic/claude-opus-4-8", "anthropic/claude-sonnet-5")


def test_parse_list_models_no_header_is_empty() -> None:
    # When unauthenticated pi prints a message with no table header -> no offer set.
    assert _parse_list_models("No models available. Use /login to authenticate.\n") == ()


def test_parse_list_models_empty_output_is_empty() -> None:
    assert _parse_list_models("") == ()


# resolver


def test_list_offered_models_defaults_to_whole_catalog_for_non_pi(tmp_path: Path) -> None:
    # The base resolver hook returns None (offer the whole catalog); only a dynamic,
    # account-gated harness overrides it.
    assert ClaudeModelResolver.build(_agent_info(tmp_path)).list_offered_models() is None


def test_switch_writes_the_control_file_and_wakes_the_agent(tmp_path: Path) -> None:
    woken: list[str] = []
    resolver = PiModelResolver.build(_agent_info(tmp_path), start_agent_fn=woken.append)
    result = resolver.switch(
        ModelIdentity(model_id="anthropic/claude-opus-4-8", effort="high", fast=False),
        frozenset({ModelAxis.MODEL}),
        lambda line: True,
    )
    assert result.ok
    # The wake happens AFTER the mailbox write (a stopped agent's session-start
    # consume must see the intent) and is token-free (process start, no message).
    assert woken == [_agent_info(tmp_path).name]
    # The mailbox is a single JSON object, not a JSONL log line.
    assert json.loads((tmp_path / "pi_control.json").read_text()) == {
        "model_id": "anthropic/claude-opus-4-8",
        "thinking_level": "high",
    }


def test_switch_twice_leaves_only_the_latest_intent(tmp_path: Path) -> None:
    # Single-slot mailbox: a second switch atomically overwrites the first (last wins),
    # so the file holds exactly one intent -- the newest -- rather than an appended log.
    resolver = PiModelResolver.build(_agent_info(tmp_path), start_agent_fn=lambda name: None)
    resolver.switch(
        ModelIdentity(model_id="anthropic/claude-opus-4-8", effort="low", fast=False),
        frozenset({ModelAxis.MODEL}),
        lambda line: True,
    )
    result = resolver.switch(
        ModelIdentity(model_id="openai/gpt-5", effort="high", fast=False),
        frozenset({ModelAxis.MODEL, ModelAxis.EFFORT}),
        lambda line: True,
    )
    assert result.ok
    control_path = tmp_path / "pi_control.json"
    # Exactly one intent in the file (a JSONL append would leave two lines).
    assert len(control_path.read_text().splitlines()) == 1
    assert json.loads(control_path.read_text()) == {"model_id": "openai/gpt-5", "thinking_level": "high"}


# the locked atomic flush writer
# The send-in-flight branch (a held ``message.lock`` -> SEND_IN_FLIGHT, no write) is pinned in
# server_test.py beside its codex sibling, where the bounded-wait constant is patched down; the
# uncontended path is unit-tested here.


def test_flush_appends_the_sentinel_when_no_send_is_in_flight(tmp_path: Path) -> None:
    status = flush_pi_queue_atomic(tmp_path)
    assert status == PiFlushTapStatus.TAPPED
    assert (tmp_path / "pi_inbox").read_text().splitlines() == ['{"minds_interrupt": true}']


def _write_models_file(config_dir: Path, providers: dict[str, Any]) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "models.json").write_text(json.dumps({"providers": providers}))


def test_custom_models_are_tagged_the_way_pi_reports_them(tmp_path: Path) -> None:
    """The live model reads `provider/model`, so the option has to carry the same tag to match it."""
    _write_models_file(tmp_path, {"local-model": {"baseUrl": "http://x/v1", "models": [{"id": "qwen2.5:7b"}]}})

    (option,) = read_custom_model_options(tmp_path)

    assert option.id == "local-model/qwen2.5:7b"
    assert option.label == "local-model/qwen2.5:7b"
    assert [effort.level for effort in option.efforts] == ["off"]
    assert option.in_picker


@pytest.mark.parametrize(
    "content",
    [
        None,
        b"not json",
        b"[]",
        b'{"providers": []}',
        b'{"providers": {"p": {"models": 3}}}',
        pytest.param(b'{"providers": {"p": {"models": [{"id": "caf\xe9"}]}}}', id="not-text"),
    ],
)
def test_a_missing_or_unreadable_models_file_adds_no_models(tmp_path: Path, content: bytes | None) -> None:
    if content is not None:
        (tmp_path / "models.json").write_bytes(content)

    assert read_custom_model_options(tmp_path) == ()


def test_an_account_on_a_local_model_offers_that_model_and_another_pi_account_the_catalog(tmp_path: Path) -> None:
    local, keyed = tmp_path / "local", tmp_path / "keyed"
    keyed.mkdir()
    _write_models_file(local, {"local-model": {"models": [{"id": "qwen2.5:7b"}]}})

    offered = PiModelResolver.list_account_options(local)

    assert offered is not None
    assert [option.id for option in offered] == ["local-model/qwen2.5:7b"]
    assert PiModelResolver.list_account_options(keyed) is None


def test_a_custom_reasoning_model_offers_its_thinking_levels(tmp_path: Path) -> None:
    """Read by the same rule as the bundled catalog, so the effort control offers what pi will take."""
    model = {"id": "qwen3:8b", "reasoning": True, "thinkingLevelMap": {"off": "none"}}
    _write_models_file(tmp_path, {"local-model": {"models": [model]}})

    (option,) = read_custom_model_options(tmp_path)

    assert [effort.level for effort in option.efforts] == ["off", "minimal", "low", "medium", "high"]


def _local_model_account() -> LocalModelAccount:
    return LocalModelAccount(
        is_context_managed=True,
        records=(
            LocalModelRecord(
                id="qwen3-40k:latest",
                base_id="qwen3:8b",
                context_window=40_960,
                is_reasoning=True,
                reasoning_off_effort="none",
                is_checked=True,
            ),
            LocalModelRecord(id="llama3.3:latest-ctx65536", base_id="llama3.3:latest", context_window=65_536),
            LocalModelRecord(id="llama3.3:latest-ctx131072", base_id="llama3.3:latest", context_window=131_072),
        ),
    )


def test_a_local_model_account_s_options_carry_each_model_s_name_and_size_apart(tmp_path: Path) -> None:
    """A picker offers the model once and its sizes beside it, which the tag alone does not let it do."""
    write_account_models(tmp_path, "http://127.0.0.1:11434/v1", _local_model_account())

    options = read_custom_model_options(tmp_path)

    assert [(option.id, option.label, option.family, option.context_window) for option in options] == [
        ("local-model/qwen3-40k:latest", "qwen3:8b (40K)", "qwen3:8b", 40_960),
        ("local-model/llama3.3:latest-ctx65536", "llama3.3:latest (64K)", "llama3.3:latest", 65_536),
        ("local-model/llama3.3:latest-ctx131072", "llama3.3:latest (128K)", "llama3.3:latest", 131_072),
    ]
    assert [effort.level for effort in options[0].efforts] == ["off", "minimal", "low", "medium", "high"]
    assert [effort.level for effort in options[1].efforts] == ["off"]


def test_a_local_model_chat_s_picker_is_offered_the_server_s_models_asked_for_afresh(tmp_path: Path) -> None:
    asked: list[Path] = []

    def refresh(config_dir: Path) -> LocalModelAccount | None:
        asked.append(config_dir)
        return _local_model_account()

    resolver = PiModelResolver.build(
        _agent_info(tmp_path), start_agent_fn=lambda name: None, refresh_local_models=refresh
    )

    offered = resolver.list_offered_options()

    assert offered is not None
    assert [option.family for option in offered] == ["qwen3:8b", "llama3.3:latest", "llama3.3:latest"]
    assert asked == [tmp_path / "plugin" / "pi_coding"]


def test_any_other_pi_chat_s_picker_is_offered_the_catalog(tmp_path: Path) -> None:
    resolver = PiModelResolver.build(
        _agent_info(tmp_path), start_agent_fn=lambda name: None, refresh_local_models=lambda config_dir: None
    )

    assert resolver.list_offered_options() is None


def test_a_switch_to_a_local_model_readies_it_before_pi_hears_of_the_switch(tmp_path: Path) -> None:
    readied: list[tuple[Path, str, bool]] = []
    control_path = tmp_path / "pi_control.json"
    resolver = PiModelResolver.build(
        _agent_info(tmp_path),
        start_agent_fn=lambda name: None,
        prepare_local_model=lambda config_dir, model_id: readied.append((config_dir, model_id, control_path.exists())),
    )
    identity = ModelIdentity(model_id="local-model/llama3.3:latest-ctx65536", effort="off", fast=False)

    result = resolver.switch(identity, frozenset({ModelAxis.MODEL}), lambda line: True)

    assert result.ok
    assert readied == [(tmp_path / "plugin" / "pi_coding", "llama3.3:latest-ctx65536", False)]
    assert json.loads(control_path.read_text())["model_id"] == "local-model/llama3.3:latest-ctx65536"


def test_a_local_model_that_cannot_run_a_chat_is_refused_and_pi_is_told_nothing(tmp_path: Path) -> None:
    def refuse(config_dir: Path, model_id: str) -> None:
        raise LocalModelError(f"{model_id} answered without calling a tool.")

    woken: list[str] = []
    resolver = PiModelResolver.build(_agent_info(tmp_path), start_agent_fn=woken.append, prepare_local_model=refuse)
    identity = ModelIdentity(model_id="local-model/qwen2.5-coder:32b-ctx65536", effort="off", fast=False)

    result = resolver.switch(identity, frozenset({ModelAxis.MODEL}), lambda line: True)

    assert (result.ok, result.detail) == (False, "qwen2.5-coder:32b-ctx65536 answered without calling a tool.")
    assert not (tmp_path / "pi_control.json").exists()
    assert woken == []


def test_a_change_of_effort_alone_or_to_a_model_of_another_provider_readies_nothing(tmp_path: Path) -> None:
    readied: list[str] = []
    resolver = PiModelResolver.build(
        _agent_info(tmp_path),
        start_agent_fn=lambda name: None,
        prepare_local_model=lambda config_dir, model_id: readied.append(model_id),
    )

    resolver.switch(
        ModelIdentity(model_id="local-model/qwen3-40k:latest", effort="high", fast=False),
        frozenset({ModelAxis.EFFORT}),
        lambda line: True,
    )
    resolver.switch(
        ModelIdentity(model_id="openrouter/qwen/qwen3", effort="off", fast=False),
        frozenset({ModelAxis.MODEL}),
        lambda line: True,
    )

    assert readied == []
