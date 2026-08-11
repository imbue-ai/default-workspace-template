"""Unit tests for the Codex model resolver's switch side, a live-state conformance check, and the
root-thread bind helper.

The live READ is harness-neutral (the shared reader), so the resolver only owns switching. Switch
applies over the app-server (``thread/settings/update``): the tests inject a scripted client that
records the ``settings_update`` kwargs, proving each changed axis maps to the right field and the
pane send is never used. The conformance test pins the reader against the uniform ``{model, effort,
fast}`` schema the ledger mirrors from ``thread/settings/updated``.

codex's stop/flush are no longer control-line writers: the live ledger
(:mod:`~imbue.system_interface.harnesses.codex.ledger`) owns interrupt (``turn/interrupt`` + a
per-id settle) and the shoulder-tap gate, exercised in ``ledger_test.py``; the endpoint-level
dispatch (codex routing through the ledger, HTTP mapping) lives in ``server_test.py``.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.codex_config import APP_SERVER_THREAD_FILENAME
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.codex.model import CODEX_CATALOG
from imbue.system_interface.harnesses.codex.model import CODEX_STATE_RELATIVE_PATH
from imbue.system_interface.harnesses.codex.model import CodexModelResolver
from imbue.system_interface.harnesses.codex.model import _bind_root_thread
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


class _RecordingClient:
    """A scripted app-server client that records ``settings_update`` kwargs and never touches the
    pane. ``fail`` makes ``settings_update`` raise, standing in for an unreachable daemon."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self._fail = fail

    def settings_update(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self._fail:
            raise CodexAppServerError("daemon unreachable")

    def close(self) -> None:
        self.closed = True


def _resolver_over(tmp_path: Path, client: _RecordingClient) -> tuple[CodexModelResolver, dict[str, int]]:
    """Build a resolver whose switch connection yields ``client`` (counting opens)."""
    opens = {"n": 0}

    def _open() -> Any:
        opens["n"] += 1
        return client

    return CodexModelResolver.build(_agent_info(tmp_path), open_client=_open), opens


def _forbidden_send(_line: str) -> bool:
    pytest.fail("codex switch must apply settings over the app-server, not the pane send")


def test_switch_model_and_effort_updates_both_settings(tmp_path: Path) -> None:
    # Codex applies the change over thread/settings/update, one call carrying every changed axis.
    client = _RecordingClient()
    resolver, opens = _resolver_over(tmp_path, client)
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-terra", effort="high", fast=False),
        frozenset({ModelAxis.MODEL, ModelAxis.EFFORT}),
        _forbidden_send,
    )
    assert result.ok
    assert client.calls == [{"model": "gpt-5.6-terra", "effort": "high"}]
    assert opens["n"] == 1
    assert client.closed is True


def test_switch_effort_only_sends_only_effort(tmp_path: Path) -> None:
    # Only the changed axis is included; an omitted field leaves that setting unchanged.
    client = _RecordingClient()
    resolver, _opens = _resolver_over(tmp_path, client)
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="xhigh", fast=False),
        frozenset({ModelAxis.EFFORT}),
        _forbidden_send,
    )
    assert result.ok
    assert client.calls == [{"effort": "xhigh"}]


def test_switch_fast_on_maps_to_priority_service_tier(tmp_path: Path) -> None:
    client = _RecordingClient()
    resolver, _opens = _resolver_over(tmp_path, client)
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="medium", fast=True),
        frozenset({ModelAxis.FAST}),
        _forbidden_send,
    )
    assert result.ok
    assert client.calls == [{"service_tier": "priority"}]


def test_switch_fast_off_clears_the_service_tier(tmp_path: Path) -> None:
    # Fast off clears the tier (None) back to the default, non-fast tier.
    client = _RecordingClient()
    resolver, _opens = _resolver_over(tmp_path, client)
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="medium", fast=False),
        frozenset({ModelAxis.FAST}),
        _forbidden_send,
    )
    assert result.ok
    assert client.calls == [{"service_tier": None}]


def test_switch_with_no_axes_opens_no_connection(tmp_path: Path) -> None:
    client = _RecordingClient()
    resolver, opens = _resolver_over(tmp_path, client)
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="medium", fast=False),
        frozenset(),
        _forbidden_send,
    )
    assert result.ok
    assert client.calls == []
    assert opens["n"] == 0


def test_switch_reports_failure_when_the_daemon_is_unreachable(tmp_path: Path) -> None:
    # A transport/daemon failure surfaces as ok=False (and still closes the connection). An
    # unavailable MODEL is a different, non-erroring path (the daemon echoes its fallback).
    client = _RecordingClient(fail=True)
    resolver, _opens = _resolver_over(tmp_path, client)
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="medium", fast=False),
        frozenset({ModelAxis.MODEL}),
        _forbidden_send,
    )
    assert result.ok is False
    assert result.detail is not None
    assert client.closed is True


# =============================================================================
# Root-thread binding for the short-lived switch connection
# =============================================================================


class _BindFakeClient:
    """Records how the switch connection binds a root thread from the loaded set."""

    def __init__(self, loaded: tuple[str, ...]) -> None:
        self._loaded = loaded
        self.bound: str | None = None
        self.resumed: str | None = None

    def thread_loaded_list(self) -> tuple[str, ...]:
        return self._loaded

    def bind_thread(self, thread_id: str) -> None:
        self.bound = thread_id

    def thread_resume(self, thread_id: str) -> Any:
        self.resumed = thread_id
        return None


def _write_persisted_thread_id(tmp_path: Path, thread_id: str) -> None:
    (tmp_path / APP_SERVER_THREAD_FILENAME).write_text(thread_id, encoding="utf-8")


def test_bind_prefers_a_loaded_persisted_thread(tmp_path: Path) -> None:
    _write_persisted_thread_id(tmp_path, "root-1")
    client = _BindFakeClient(loaded=("root-1", "sub-2"))
    _bind_root_thread(client, tmp_path)
    assert client.bound == "root-1"
    assert client.resumed is None


def test_bind_resumes_a_persisted_thread_not_currently_loaded(tmp_path: Path) -> None:
    _write_persisted_thread_id(tmp_path, "root-1")
    client = _BindFakeClient(loaded=("sub-2",))
    _bind_root_thread(client, tmp_path)
    assert client.resumed == "root-1"
    assert client.bound is None


def test_bind_adopts_the_single_loaded_thread_without_a_persisted_id(tmp_path: Path) -> None:
    client = _BindFakeClient(loaded=("only-1",))
    _bind_root_thread(client, tmp_path)
    assert client.bound == "only-1"


def test_bind_raises_when_no_unambiguous_root(tmp_path: Path) -> None:
    client = _BindFakeClient(loaded=("a", "b"))
    with pytest.raises(CodexAppServerError):
        _bind_root_thread(client, tmp_path)
