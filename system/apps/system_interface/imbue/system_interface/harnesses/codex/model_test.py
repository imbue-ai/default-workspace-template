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
from imbue.mngr_codex.app_server_client import CodexModel
from imbue.mngr_codex.codex_config import APP_SERVER_THREAD_FILENAME
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.codex.model import CODEX_CATALOG
from imbue.system_interface.harnesses.codex.model import CODEX_STATE_RELATIVE_PATH
from imbue.system_interface.harnesses.codex.model import CodexModelResolver
from imbue.system_interface.harnesses.codex.model import _bind_root_thread
from imbue.system_interface.harnesses.codex.model import _subscribe_root_thread
from imbue.system_interface.harnesses.codex.model import codex_model_to_option
from imbue.system_interface.harnesses.codex.model import codex_models_to_options
from imbue.system_interface.harnesses.codex.model import get_codex_model_options_path
from imbue.system_interface.harnesses.codex.model import read_codex_model_options
from imbue.system_interface.harnesses.codex.model import write_codex_model_options
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import match_option
from imbue.system_interface.harnesses.model import model_state_path
from imbue.system_interface.harnesses.model import read_model_identity


def _codex_model(
    model: str,
    display_name: str,
    efforts: tuple[str, ...],
    *,
    tiers: tuple[str, ...] = (),
    hidden: bool = False,
) -> CodexModel:
    """A ``model/list`` entry for tests (id == model, given per-model efforts and service tiers).

    Built from the wire (alias) shape via ``model_validate`` so it matches what the daemon sends."""
    return CodexModel.model_validate(
        {
            "id": model,
            "model": model,
            "displayName": display_name,
            "hidden": hidden,
            "supportedReasoningEfforts": [{"reasoningEffort": level} for level in efforts],
            "serviceTiers": [{"id": tier} for tier in tiers],
        }
    )


def _agent_info(tmp_path: Path) -> AgentInfo:
    return AgentInfo(
        id="agent-1",
        name="a",
        state="RUNNING",
        agent_state_dir=tmp_path,
        claude_config_dir=tmp_path / "unused",
        harness=HarnessType.CODEX,
    )


def test_catalog_is_on_change_and_dynamic_with_no_static_options() -> None:
    # Codex is fully dynamic: ON_CHANGE (interactive, no optimistic overlay), a DYNAMIC per-agent
    # picker, and NO static options (the model set comes from the daemon's model/list).
    assert CODEX_CATALOG.switch_mode == SwitchMode.ON_CHANGE
    assert CODEX_CATALOG.picker_mode == PickerMode.DYNAMIC
    assert CODEX_CATALOG.options == ()
    assert CODEX_CATALOG.powered_by_text == "Powered by Codex"
    assert CODEX_CATALOG.native_atomic_shoulder_tap_possible is True


def test_model_mapper_pulls_efforts_and_fast_per_model() -> None:
    # The one pure mapper: id=model, label=display_name, efforts verbatim, fast iff a priority tier,
    # in_picker iff not hidden.
    fast_model = _codex_model("gpt-5.6-sol", "GPT-5.6-Sol", ("low", "high", "ultra"), tiers=("priority", "default"))
    option = codex_model_to_option(fast_model)
    assert option.id == "gpt-5.6-sol"
    assert option.label == "GPT-5.6-Sol"
    assert tuple(effort.level for effort in option.efforts) == ("low", "high", "ultra")
    assert option.supports_fast is True
    assert option.in_picker is True

    # A model without a priority tier does not support fast; a hidden model is matchable but never offered.
    plain = _codex_model("gpt-5.2", "GPT-5.2", ("low", "xhigh"), tiers=())
    plain_option = codex_model_to_option(plain)
    assert plain_option.supports_fast is False
    hidden = _codex_model("gpt-secret", "Secret", ("medium",), hidden=True)
    assert codex_model_to_option(hidden).in_picker is False


def test_state_relative_path_is_under_codex_home() -> None:
    # The registered relative dir must resolve to the same place get_codex_home does, so the
    # shared reader finds the file the patched codex writes under CODEX_HOME.
    assert model_state_path(Path("/agent"), CODEX_STATE_RELATIVE_PATH) == get_codex_home(Path("/agent")) / (
        "model_state.json"
    )


def test_live_identity_matches_the_per_agent_model_set(tmp_path: Path) -> None:
    # The reader is unchanged (uniform {model, effort, fast}); the chip-match is now against the
    # PER-AGENT options (mapped from model/list), not a static catalog. A live read matches the
    # mapped option, with per-model efforts/fast honored.
    state_path = model_state_path(tmp_path, CODEX_STATE_RELATIVE_PATH)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"model": "gpt-5.6-sol", "effort": "high", "fast": True}))
    identity = read_model_identity(state_path)
    assert identity == ModelIdentity(model_id="gpt-5.6-sol", effort="high", fast=True)

    options = codex_models_to_options(
        (
            _codex_model("gpt-5.6-sol", "GPT-5.6-Sol", ("low", "high", "max"), tiers=("priority",)),
            _codex_model("gpt-5.2", "GPT-5.2", ("low", "xhigh")),
        )
    )
    assert identity is not None
    matched = match_option(identity, options)
    assert matched is not None
    assert matched.id == "gpt-5.6-sol"
    assert matched.label == "GPT-5.6-Sol"
    # fast on a no-priority model is a shrug (per-model fast, daemon-sourced).
    assert match_option(ModelIdentity(model_id="gpt-5.2", effort="low", fast=True), options) is None


class _RecordingClient:
    """A scripted app-server client that records ``settings_update`` kwargs and never touches the
    pane. ``fail`` makes ``settings_update`` raise, standing in for an unreachable daemon.
    ``models`` is returned from ``model_list`` (for the dynamic-options fetch)."""

    def __init__(self, fail: bool = False, models: tuple[CodexModel, ...] = ()) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self._fail = fail
        self._models = models

    def settings_update(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self._fail:
            raise CodexAppServerError("daemon unreachable")

    def model_list(self) -> tuple[CodexModel, ...]:
        if self._fail:
            raise CodexAppServerError("daemon unreachable")
        return self._models

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


def test_switch_on_model_change_sends_all_three_axes(tmp_path: Path) -> None:
    # A MODEL-axis change (re)asserts model + effort + fast TOGETHER, even for a fast=False pick that
    # changed only model + effort: the daemon does not enforce tiers, so service_tier must be sent
    # explicitly (None here) to clear any stale priority on the switch.
    client = _RecordingClient()
    resolver, opens = _resolver_over(tmp_path, client)
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-terra", effort="high", fast=False),
        frozenset({ModelAxis.MODEL, ModelAxis.EFFORT}),
        _forbidden_send,
    )
    assert result.ok
    assert client.calls == [{"model": "gpt-5.6-terra", "effort": "high", "service_tier": None}]
    assert opens["n"] == 1
    assert client.closed is True


def test_switch_to_a_no_priority_model_clears_a_stale_fast(tmp_path: Path) -> None:
    # Even when only the model axis is reported changed (effort/fast unchanged on the frontend), a
    # codex model switch re-asserts all three, so switching to a model that dropped fast clears the
    # service tier rather than leaving a stale priority the daemon would keep.
    client = _RecordingClient()
    resolver, _opens = _resolver_over(tmp_path, client)
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.2", effort="low", fast=False),
        frozenset({ModelAxis.MODEL}),
        _forbidden_send,
    )
    assert result.ok
    assert client.calls == [{"model": "gpt-5.2", "effort": "low", "service_tier": None}]


def test_list_offered_options_maps_model_list_fresh(tmp_path: Path) -> None:
    # The dynamic picker fetches model/list fresh and maps each entry to a full ModelOption.
    models = (
        _codex_model("gpt-5.6-sol", "GPT-5.6-Sol", ("low", "high"), tiers=("priority",)),
        _codex_model("gpt-5.2", "GPT-5.2", ("low", "xhigh")),
    )
    client = _RecordingClient(models=models)
    resolver, opens = _resolver_over(tmp_path, client)
    options = resolver.list_offered_options()
    assert options is not None
    assert [opt.id for opt in options] == ["gpt-5.6-sol", "gpt-5.2"]
    assert options[0].supports_fast is True
    assert options[1].supports_fast is False
    assert opens["n"] == 1
    assert client.closed is True


def test_list_offered_options_tolerates_a_daemon_failure(tmp_path: Path) -> None:
    # A model/list failure yields an empty (non-None) tuple: the picker shows no models, never a crash.
    client = _RecordingClient(fail=True)
    resolver, _opens = _resolver_over(tmp_path, client)
    assert resolver.list_offered_options() == ()
    assert client.closed is True


# =============================================================================
# The raw model-options sidecar (offline-restart persistence)
# =============================================================================


def test_write_then_read_round_trips_the_raw_model_list(tmp_path: Path) -> None:
    # The sidecar persists the RAW CodexModel entries (by alias) and reads them back byte-identical,
    # so the mapping (codex_models_to_options) is applied on READ, not at write time.
    models = (
        _codex_model("gpt-5.6-terra", "GPT-5.6-Terra", ("high",), tiers=("priority",)),
        _codex_model("gpt-5.2", "GPT-5.2", ("low", "xhigh")),
    )
    path = get_codex_model_options_path(tmp_path)
    write_codex_model_options(path, models)
    assert read_codex_model_options(path) == models
    # Mapping happens on read, yielding the same per-agent options the live fetch would.
    assert [opt.id for opt in codex_models_to_options(read_codex_model_options(path))] == [
        "gpt-5.6-terra",
        "gpt-5.2",
    ]


def test_read_model_options_missing_or_non_list_is_empty(tmp_path: Path) -> None:
    # An absent sidecar, and a present-but-malformed payload, both read as () -> the chip falls back
    # to rendering no slots rather than crashing.
    assert read_codex_model_options(get_codex_model_options_path(tmp_path)) == ()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"models": "not-a-list"}))
    assert read_codex_model_options(bad) == ()
    bad.write_text("{ this is not json")
    assert read_codex_model_options(bad) == ()


def test_read_model_options_skips_a_malformed_entry(tmp_path: Path) -> None:
    # A single entry that no longer matches the CodexModel shape is skipped; the valid ones survive,
    # so a daemon schema drift degrades gracefully instead of dropping the whole file.
    good = _codex_model("gpt-5.6-terra", "GPT-5.6-Terra", ("high",))
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"models": [good.model_dump(by_alias=True), {"model": 123}, "not-a-dict"]}))
    read_back = read_codex_model_options(path)
    assert [model.model for model in read_back] == ["gpt-5.6-terra"]


def test_list_offered_options_persists_the_sidecar_on_a_live_fetch(tmp_path: Path) -> None:
    # Write-through (picker-open path): a successful model/list fetch persists the raw list to the
    # sidecar, so the chip resolves offline after a restart. The mapped options are still returned.
    models = (_codex_model("gpt-5.6-terra", "GPT-5.6-Terra", ("high",), tiers=("priority",)),)
    client = _RecordingClient(models=models)
    resolver, _opens = _resolver_over(tmp_path, client)
    options = resolver.list_offered_options()
    assert options is not None and [opt.id for opt in options] == ["gpt-5.6-terra"]
    assert read_codex_model_options(get_codex_model_options_path(tmp_path)) == models


def test_list_offered_options_failure_does_not_clobber_a_good_sidecar(tmp_path: Path) -> None:
    # A failed fetch must never overwrite a good sidecar: the last-known raw list survives so the
    # chip keeps matching offline.
    good = (_codex_model("gpt-5.6-terra", "GPT-5.6-Terra", ("high",)),)
    path = get_codex_model_options_path(tmp_path)
    write_codex_model_options(path, good)
    client = _RecordingClient(fail=True)
    resolver, _opens = _resolver_over(tmp_path, client)
    assert resolver.list_offered_options() == ()
    assert read_codex_model_options(path) == good


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


# =============================================================================
# Root-thread SUBSCRIBING for the persistent live connection
# =============================================================================
#
# The live-connection path must RESUME (thread/resume) rather than bind, because only a resume
# subscribes the connection to the thread's turn/*/item/* event stream. Unlike the switch path
# (above), it resumes even a thread the daemon still holds loaded -- a bind would be local-only and
# leave the ledger deaf to delivery/reconcile.


def test_subscribe_resumes_a_persisted_thread_even_when_loaded(tmp_path: Path) -> None:
    # The key divergence from the switch path: a persisted root that IS loaded is still resumed
    # (bind would skip the RPC and never subscribe), so the connection joins the event stream.
    _write_persisted_thread_id(tmp_path, "root-1")
    client = _BindFakeClient(loaded=("root-1", "sub-2"))
    _subscribe_root_thread(client, tmp_path)
    assert client.resumed == "root-1"
    assert client.bound is None


def test_subscribe_resumes_a_persisted_thread_not_loaded(tmp_path: Path) -> None:
    _write_persisted_thread_id(tmp_path, "root-1")
    client = _BindFakeClient(loaded=("sub-2",))
    _subscribe_root_thread(client, tmp_path)
    assert client.resumed == "root-1"
    assert client.bound is None


def test_subscribe_adopts_and_resumes_the_single_loaded_thread(tmp_path: Path) -> None:
    client = _BindFakeClient(loaded=("only-1",))
    _subscribe_root_thread(client, tmp_path)
    assert client.resumed == "only-1"
    assert client.bound is None


def test_subscribe_raises_when_no_unambiguous_root(tmp_path: Path) -> None:
    client = _BindFakeClient(loaded=("a", "b"))
    with pytest.raises(CodexAppServerError):
        _subscribe_root_thread(client, tmp_path)
