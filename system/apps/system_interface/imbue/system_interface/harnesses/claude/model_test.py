"""Unit tests for the Claude model resolver's switch side (the live read is shared)."""

from pathlib import Path

from imbue.system_interface.agent_discovery import AgentInfo
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


def test_catalog_options_carry_suffix_free_reported_ids() -> None:
    # The matcher keys off harness_reported_model_id; the statusline reports suffix-free API ids.
    reported = {option.id: option.harness_reported_model_id for option in CLAUDE_CATALOG.options}
    assert reported == {
        "opus[1m]": "claude-opus-5",
        "sonnet": "claude-sonnet-5",
        "haiku": "claude-haiku-4-5",
    }


def test_live_statusline_model_ids_match_their_catalog_option() -> None:
    # The model ids claude 2.1.227's statusline actually reports, captured from a live
    # binary launched exactly as the workspace launches it (settings.json model="opus[1m]",
    # then /model sonnet, /model haiku). Only sonnet reports the bare catalog key: opus keeps
    # its [1m] launch suffix and haiku reports a dated id, so two of the three reach their
    # option through match_option's prefix pass rather than an exact key hit.
    for reported_id, expected_label in (
        ("claude-opus-5[1m]", "Opus 5 (1M)"),
        ("claude-sonnet-5", "Sonnet 5"),
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
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
        ModelIdentity(model_id="sonnet", effort="high", fast=False),
        frozenset({ModelAxis.MODEL, ModelAxis.EFFORT}),
        send,
    )

    assert result.ok
    assert sent == ["/model sonnet", "/effort high"]


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
