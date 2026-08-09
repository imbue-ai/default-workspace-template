"""Unit tests for the Codex model resolver's switch side plus a live-state conformance check.

The live READ is harness-neutral (the shared reader), so the resolver only owns switching.
The conformance test pins the reader against the codex-in-minds patch's NEW output schema --
CI cannot execute the Rust binary, so the fixture is hand-written to that schema. The reader's
graceful handling of the OLD schema (``reasoning_effort``/``service_tier``) lives in
``harnesses/model_test.py``.
"""

import json
from pathlib import Path

from imbue.mngr_codex.codex_config import get_codex_home
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.codex.model import CODEX_CATALOG
from imbue.system_interface.harnesses.codex.model import CODEX_STATE_RELATIVE_PATH
from imbue.system_interface.harnesses.codex.model import CodexModelResolver
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import match_option
from imbue.system_interface.harnesses.model import model_state_path
from imbue.system_interface.harnesses.model import read_model_identity


def _agent_info(tmp_path: Path) -> AgentInfo:
    return AgentInfo(
        id="agent-1",
        name="a",
        state="RUNNING",
        agent_state_dir=tmp_path,
        claude_config_dir=tmp_path / "unused",
        harness=HarnessType.CODEX,
    )


def test_catalog_is_eager_then_reconcile() -> None:
    assert CODEX_CATALOG.switch_mode == SwitchMode.EAGER_THEN_RECONCILE


def test_state_relative_path_is_under_codex_home() -> None:
    # The registered relative dir must resolve to the same place get_codex_home does, so the
    # shared reader finds the file the patched codex writes under CODEX_HOME.
    assert model_state_path(Path("/agent"), CODEX_STATE_RELATIVE_PATH) == get_codex_home(Path("/agent")) / (
        "minds_model_state.json"
    )


def test_reader_matches_the_new_patch_schema(tmp_path: Path) -> None:
    # A hand-written fixture of the codex-in-minds patch's NEW {model, effort, fast} output.
    state_path = model_state_path(tmp_path, CODEX_STATE_RELATIVE_PATH)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"model": "gpt-5.6-sol", "effort": "high", "fast": True}))
    identity = read_model_identity(state_path)
    assert identity is not None
    assert identity == ModelIdentity(model_id="gpt-5.6-sol", effort="high", fast=True)
    matched = match_option(identity, CODEX_CATALOG.options)
    assert matched is not None
    assert matched.id == "gpt-5.6-sol"


def test_switch_model_and_effort_send_one_model_command(tmp_path: Path) -> None:
    # Codex applies model + effort together, so a model change sends one
    # `/model <model> <effort>` -- not a separate /effort.
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-terra", effort="high", fast=False),
        frozenset({ModelAxis.MODEL, ModelAxis.EFFORT}),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == ["/model gpt-5.6-terra high"]


def test_switch_effort_only_still_goes_through_model(tmp_path: Path) -> None:
    # Effort has no standalone codex command; it rides /model with the current model.
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="xhigh", fast=False),
        frozenset({ModelAxis.EFFORT}),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == ["/model gpt-5.6-sol xhigh"]


def test_switch_fast_toggle_sends_only_fast(tmp_path: Path) -> None:
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="medium", fast=True),
        frozenset({ModelAxis.FAST}),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == ["/fast on"]


def test_switch_with_no_axes_sends_nothing(tmp_path: Path) -> None:
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="medium", fast=False),
        frozenset(),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == []
