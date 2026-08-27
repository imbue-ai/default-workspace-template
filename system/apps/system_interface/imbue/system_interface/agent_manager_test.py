"""Tests for the AgentManager."""

import json
import os
import queue
import shutil
import signal
import tomllib
import threading
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest
from mngr_cli_contract.contract import assert_mngr_argv_valid
from oom_priority import bands
from watchdog.events import FileClosedNoWriteEvent
from watchdog.events import FileModifiedEvent
from watchdog.events import FileMovedEvent
from watchdog.events import FileOpenedEvent

from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.observe import make_agent_removed_event
from imbue.mngr.api.observe import make_agent_state_event
from imbue.mngr.api.observe import make_full_agent_state_event
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.primitives import AgentId as MngrAgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName as MngrAgentName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_codex.app_server_client import CodexModel
from imbue.system_interface import client_activity
from imbue.system_interface import projects
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.agent_manager import _LogQueueCallback
from imbue.system_interface.accounts import commit_account
from imbue.system_interface.accounts import mint_account_dir
from imbue.system_interface.agent_manager import _build_chat_create_command
from imbue.system_interface.agent_manager import _build_chat_display_label_command
from imbue.system_interface.agent_manager import _build_chat_rename_command
from imbue.system_interface.agent_manager import _build_observe_command_argv
from imbue.system_interface.agent_manager import _chat_project_label
from imbue.system_interface.agent_manager import _make_apps_file_handler
from imbue.system_interface.agent_manager import _rename_failure_detail
from imbue.system_interface.harnesses.codex.activity import CodexActivityTracker
from imbue.system_interface.harnesses.codex.model import codex_models_to_options
from imbue.system_interface.harnesses.codex.model import get_codex_model_options_path
from imbue.system_interface.harnesses.codex.model import write_codex_model_options
from imbue.system_interface.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.system_interface.harnesses.events import SpecialEventKind
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.registry import get_model_state_path
from imbue.system_interface.harnesses.session import FileHarnessSession
from imbue.system_interface.models import AgentCreationError
from imbue.system_interface.models import AgentNameConflictError
from imbue.system_interface.models import AgentRenameError
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.models import AppEntry
from imbue.system_interface.models import QueuedMessageState
from imbue.system_interface.oom_prioritizer import ChatOomPrioritizer
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# Several tests in this module spin up real watchdog FSEvents observers
# (via ``_start_app_watcher``). On macOS the FSEvents emitter thread
# occasionally stalls during shutdown, tripping pytest-timeout. Mark the
# whole file as flaky so offload retries it automatically -- mirrors
# ``ws_broadcaster_test.py``.
pytestmark = pytest.mark.flaky


def _seed_agent(
    manager: AgentManager,
    agent_id: str,
    harness: HarnessType = HarnessType.CLAUDE,
    state: str = "RUNNING",
) -> None:
    """Insert a placeholder ``AgentStateItem`` directly into the tracked map."""
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id,
            name=f"agent-{agent_id}",
            state=state,
            labels={},
            work_dir=None,
            harness=harness,
        )


_PROVIDER = ProviderInstanceName("local")


def _agent_details(
    name: str,
    agent_id: MngrAgentId | None = None,
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
    labels: dict[str, str] | None = None,
    work_dir: str = "/tmp/work",
    host_id: HostId | None = None,
    provider_name: ProviderInstanceName = _PROVIDER,
) -> AgentDetails:
    """Build an ``AgentDetails`` with controllable identity, state, and location.

    Mirrors what the observe stream carries: a real lifecycle ``state`` and a
    nested ``HostDetails`` whose id/provider are what ``_build_agent_match`` reads
    to route messages. Fields the manager never inspects are given inert defaults.
    """
    return AgentDetails(
        id=agent_id if agent_id is not None else MngrAgentId(),
        name=MngrAgentName(name),
        type="claude",
        command=CommandString("claude"),
        work_dir=Path(work_dir),
        initial_branch=None,
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=state,
        labels=labels if labels is not None else {},
        host=HostDetails(
            id=host_id if host_id is not None else HostId(),
            name="test-host",
            provider_name=provider_name,
            state=HostState.RUNNING,
        ),
    )


def _drain(q: queue.Queue[str | None]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while not q.empty():
        raw = q.get_nowait()
        if raw is None:
            break
        out.append(json.loads(raw))
    return out


def _last_agents_updated(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.get("type") == "agents_updated":
            return message
    return None


def test_get_agents_initially_empty(agent_manager: AgentManager) -> None:
    agents = agent_manager.get_agents()
    assert agents == []


def test_get_apps_initially_empty(agent_manager: AgentManager) -> None:
    apps = agent_manager.get_apps()
    assert apps == []


def test_get_proto_agents_initially_empty(agent_manager: AgentManager) -> None:
    protos = agent_manager.get_proto_agents()
    assert protos == []


def test_read_apps_parses_toml(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_content = """
[[apps]]
name = "web"
url = "http://localhost:8000"
label = "web-x7k9q2w1"

[[apps]]
name = "terminal"
url = "http://localhost:7681"
"""
    toml_file = tmp_path / "apps.toml"
    toml_file.write_text(toml_content)

    agent_manager._read_apps(toml_file)

    apps = agent_manager.get_apps()
    assert len(apps) == 2
    assert apps[0].name == "web"
    assert apps[0].url == "http://localhost:8000"
    assert apps[0].label == "web-x7k9q2w1"
    assert apps[1].name == "terminal"
    assert apps[1].url == "http://localhost:7681"
    # A row written before labels existed reads back with an empty label.
    assert apps[1].label == ""


def test_read_apps_reads_the_registered_icon(agent_manager: AgentManager, tmp_path: Path) -> None:
    icon = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M2 2h12v12H2z"/></svg>'
    toml_file = tmp_path / "apps.toml"
    toml_file.write_text(
        "[[apps]]\n"
        'name = "web"\n'
        'url = "http://localhost:8000"\n'
        'label = "web-x7k9q2w1"\n'
        f"icon = {json.dumps(icon)}\n"
        "\n"
        "[[apps]]\n"
        'name = "terminal"\n'
        'url = "http://localhost:7681"\n'
    )

    agent_manager._read_apps(toml_file)

    apps = agent_manager.get_apps()
    assert apps[0].icon == icon
    # An app that registered no icon reads back with an empty one.
    assert apps[1].icon == ""


@pytest.mark.parametrize(
    "icon",
    [
        "<svg><script>alert(1)</script></svg>",
        '<svg onload="alert(1)"></svg>',
        "<svg><style>* { display: none }</style></svg>",
        '<svg><a href="javascript:alert(1)"></a></svg>',
        "<div>not an svg</div>",
        "<svg" + " " * 20000 + "></svg>",
    ],
)
def test_read_apps_drops_an_unsafe_icon(agent_manager: AgentManager, tmp_path: Path, icon: str) -> None:
    """``forward_port.py`` never writes markup like this, but a hand-edited
    registry must not be able to push it into the client's DOM."""
    toml_file = tmp_path / "apps.toml"
    toml_file.write_text(f'[[apps]]\nname = "web"\nurl = "http://localhost:8000"\nicon = {json.dumps(icon)}\n')

    agent_manager._read_apps(toml_file)

    apps = agent_manager.get_apps()
    # The app itself still registers; only its icon is refused.
    assert len(apps) == 1
    assert apps[0].icon == ""


def test_read_apps_reads_the_internal_flag(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_file = tmp_path / "apps.toml"
    toml_file.write_text(
        "[[apps]]\n"
        'name = "owner-exec"\n'
        'url = "http://localhost:8793"\n'
        "internal = true\n"
        "\n"
        "[[apps]]\n"
        'name = "web"\n'
        'url = "http://localhost:8000"\n'
    )

    agent_manager._read_apps(toml_file)

    apps = agent_manager.get_apps()
    assert apps[0].internal is True
    # A row with no `internal` key -- every ordinary app -- reads back False.
    assert apps[1].internal is False


def test_read_apps_reads_the_program_field(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_file = tmp_path / "apps.toml"
    toml_file.write_text(
        "[[apps]]\n"
        'name = "files"\n'
        'url = "http://localhost:8300"\n'
        'program = "files"\n'
        "\n"
        "[[apps]]\n"
        'name = "web"\n'
        'url = "http://localhost:8000"\n'
    )

    agent_manager._read_apps(toml_file)

    apps = agent_manager.get_apps()
    assert apps[0].program == "files"
    # A row with no `program` key -- an unsupervised or pre-field app -- reads back "".
    assert apps[1].program == ""


def test_read_apps_carries_probed_liveness_across_a_reread(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Re-reading the registry (a registration, an icon change) must not flash
    a stopped app back to running until the next probe lands."""
    toml_file = tmp_path / "apps.toml"
    toml_file.write_text('[[apps]]\nname = "web"\nurl = "http://localhost:8000"\n')
    agent_manager._read_apps(toml_file)
    with agent_manager._lock:
        agent_manager._apps = [
            app.model_copy_update(to_update(app.field_ref().is_running, False)) for app in agent_manager._apps
        ]

    agent_manager._read_apps(toml_file)

    apps = agent_manager.get_apps()
    assert apps[0].is_running is False


def test_refresh_app_liveness_updates_entries_and_broadcasts_once(
    broadcaster: WebSocketBroadcaster,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MNGR_AGENT_ID", "test-agent-id")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", "/tmp/test-work")
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    probed_targets: list[tuple[str, str]] = []

    def fake_prober(program: str, url: str) -> bool:
        probed_targets.append((program, url))
        return program == "files"

    manager = AgentManager.build(broadcaster, liveness_prober=fake_prober)
    with manager._lock:
        manager._apps = [
            AppEntry(name="files", url="http://localhost:8300", program="files"),
            AppEntry(name="web", url="http://localhost:8000"),
        ]
    events: list[dict[str, Any]] = []
    client_queue = broadcaster.register()

    manager.refresh_app_liveness()

    while not client_queue.empty():
        message = client_queue.get_nowait()
        if message is not None:
            events.append(json.loads(message))
    apps_updated_events = [event for event in events if event.get("type") == "apps_updated"]
    assert len(apps_updated_events) == 1
    serialized_by_name = {app["name"]: app for app in apps_updated_events[0]["apps"]}
    assert serialized_by_name["files"]["is_running"] is True
    assert serialized_by_name["web"]["is_running"] is False
    assert ("files", "http://localhost:8300") in probed_targets
    assert ("", "http://localhost:8000") in probed_targets

    # A second pass with the same answers changes nothing, so nothing is broadcast.
    manager.refresh_app_liveness()
    second_pass_events = []
    while not client_queue.empty():
        second_pass_message = client_queue.get_nowait()
        if second_pass_message is not None:
            second_pass_events.append(json.loads(second_pass_message))
    assert [event for event in second_pass_events if event.get("type") == "apps_updated"] == []


def test_read_apps_handles_missing_file(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_file = tmp_path / "nonexistent.toml"
    agent_manager._read_apps(toml_file)

    apps = agent_manager.get_apps()
    assert apps == []


def test_read_apps_handles_empty_file(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_file = tmp_path / "empty.toml"
    toml_file.write_text("")
    agent_manager._read_apps(toml_file)

    apps = agent_manager.get_apps()
    assert apps == []


def test_read_apps_ignores_entries_without_name(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_content = """
[[apps]]
url = "http://localhost:8000"
"""
    toml_file = tmp_path / "apps.toml"
    toml_file.write_text(toml_content)

    agent_manager._read_apps(toml_file)

    apps = agent_manager.get_apps()
    assert apps == []


def test_get_agents_serialized(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        agent_manager._agents["a1"] = AgentStateItem(
            id="a1",
            name="agent-one",
            state="RUNNING",
            labels={"user_created": "true"},
            work_dir="/tmp/work",
        )

    serialized = agent_manager.get_agents_serialized()
    assert len(serialized) == 1
    assert serialized[0]["id"] == "a1"
    assert serialized[0]["name"] == "agent-one"
    assert serialized[0]["labels"] == {"user_created": "true"}
    assert serialized[0]["activity_state"] is None


def test_get_apps_serialized(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        agent_manager._apps = [
            AppEntry(name="web", url="http://localhost:8000", label="web-x7k9q2w1"),
        ]

    serialized = agent_manager.get_apps_serialized()
    assert serialized == [
        {
            "name": "web",
            "url": "http://localhost:8000",
            "label": "web-x7k9q2w1",
            "icon": "",
            "internal": False,
            "program": "",
            "is_running": True,
        }
    ]


def test_get_apps_serialized_carries_the_icon(agent_manager: AgentManager) -> None:
    """The icon rides alongside name/url/label everywhere the app list is sent,
    so a client can draw the app's own glyph without a second request."""
    icon = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M2 2h12v12H2z"/></svg>'
    with agent_manager._lock:
        agent_manager._apps = [
            AppEntry(name="web", url="http://localhost:8000", label="web-x7k9q2w1", icon=icon),
        ]

    serialized = agent_manager.get_apps_serialized()
    assert serialized == [
        {
            "name": "web",
            "url": "http://localhost:8000",
            "label": "web-x7k9q2w1",
            "icon": icon,
            "internal": False,
            "program": "",
            "is_running": True,
        }
    ]


def test_get_apps_serialized_carries_internal(agent_manager: AgentManager) -> None:
    """The frontend's `pickableApps` excludes an internal app everywhere it
    offers apps to open, so the flag has to reach it over the wire."""
    with agent_manager._lock:
        agent_manager._apps = [
            AppEntry(name="owner-exec", url="http://localhost:8793", internal=True),
        ]

    serialized = agent_manager.get_apps_serialized()
    assert serialized == [
        {
            "name": "owner-exec",
            "url": "http://localhost:8793",
            "label": "",
            "icon": "",
            "internal": True,
            "program": "",
            "is_running": True,
        }
    ]


def test_resolve_agent_work_dir_from_own_env(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        result = agent_manager._resolve_agent_work_dir("test-agent-id")
    assert result == "/tmp/test-work"


def test_resolve_agent_work_dir_from_tracked_agent(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        agent_manager._agents["other-agent"] = AgentStateItem(
            id="other-agent",
            name="other",
            state="RUNNING",
            labels={},
            work_dir="/tmp/other-work",
        )
        result = agent_manager._resolve_agent_work_dir("other-agent")
    assert result == "/tmp/other-work"


def test_resolve_agent_work_dir_returns_none_for_unknown(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        result = agent_manager._resolve_agent_work_dir("unknown-id")
    assert result is None


def test_create_chat_agent_broadcasts_proto_created(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """The proto_agent_created broadcast fires before the creation thread runs."""
    q = broadcaster.register()

    created = agent_manager.create_chat_agent("test-chat")
    agent_manager.stop()

    assert isinstance(created.agent_id, str)
    assert len(created.agent_id) > 0
    assert created.name == "test-chat"
    assert created.display_name == "test-chat"

    raw = q.get_nowait()
    assert raw is not None
    proto_msg = json.loads(raw)
    assert proto_msg["type"] == "proto_agent_created"
    assert proto_msg["agent_id"] == created.agent_id
    assert proto_msg["creation_type"] == "chat"
    assert proto_msg["parent_agent_id"] is None


def test_create_codex_agent_broadcasts_proto_created_with_the_chat_creation_type(
    agent_manager: AgentManager,
    broadcaster: WebSocketBroadcaster,
    git_work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both menu entries make a chat, so creation_type is the role -- never the harness."""
    # Stub the sign-in preflight to always report signed in, so the create does not depend
    # on a real (possibly signed-out) codex CLI in the test env.
    agent_manager._auth_gate = lambda check: None
    q = broadcaster.register()

    with agent_manager._lock:
        agent_manager._agents[agent_manager._own_agent_id] = AgentStateItem(
            id=agent_manager._own_agent_id,
            name="primary",
            state="RUNNING",
            labels={},
            work_dir=str(git_work_dir),
        )

    codex_account_id, _ = mint_account_dir()
    commit_account(codex_account_id, "openai", "OpenAI")
    created = agent_manager.create_chat_agent("test-codex", account_id=codex_account_id)
    agent_manager.stop()

    assert isinstance(created.agent_id, str)

    raw = q.get_nowait()
    assert raw is not None
    proto_msg = json.loads(raw)
    assert proto_msg["type"] == "proto_agent_created"
    assert proto_msg["creation_type"] == "chat"
    assert proto_msg["parent_agent_id"] is None


def test_get_log_queue_for_proto_agent(agent_manager: AgentManager, git_work_dir: Path) -> None:
    """The log queue is available immediately after create_chat_agent returns."""
    with agent_manager._lock:
        agent_manager._agents[agent_manager._own_agent_id] = AgentStateItem(
            id=agent_manager._own_agent_id,
            name="primary",
            state="RUNNING",
            labels={},
            work_dir=str(git_work_dir),
        )

    created = agent_manager.create_chat_agent("test-chat")
    log_q = agent_manager.get_log_queue(created.agent_id)
    assert log_q is not None

    agent_manager.stop()


def test_get_log_queue_returns_none_for_unknown(agent_manager: AgentManager) -> None:
    assert agent_manager.get_log_queue("nonexistent") is None


def test_stop_without_start(agent_manager: AgentManager) -> None:
    """Stopping an agent manager that was never started is safe."""
    agent_manager.stop()


def test_agent_state_event_adds_agent(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """An AGENT_STATE event for a new agent updates the agent list and broadcasts."""
    q = broadcaster.register()

    test_agent_id = MngrAgentId()
    agent = _agent_details("discovered-agent", agent_id=test_agent_id, labels={"user_created": "true"})

    agent_manager._handle_observe_event(make_agent_state_event(agent))

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)
    assert agents[0].name == "discovered-agent"

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"


def _layout_ops(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [message for message in messages if message.get("type") == "layout_op"]


def test_assist_labeled_agent_auto_opens_its_tab(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A chat spawned by the get-help flow (carrying the ``assist`` label) auto-opens its tab."""
    q = broadcaster.register()
    agent = _agent_details("assist-abc123", labels={"assist": "true"})
    agent_manager._handle_observe_event(make_agent_state_event(agent))

    messages = _drain(q)
    opens = _layout_ops(messages)
    assert len(opens) == 1
    assert opens[0]["op"] == "open"
    assert opens[0]["args"] == {"ref": "chat:assist-abc123"}
    # The agent list must be broadcast before the open, or the frontend drops the open
    # (it resolves ``chat:<name>`` against its known-agents list).
    types = [m.get("type") for m in messages]
    assert types.index("agents_updated") < types.index("layout_op")


def test_non_assist_agent_does_not_auto_open(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """An ordinary discovered agent (no ``assist`` label) does not trigger an auto-open."""
    q = broadcaster.register()
    agent = _agent_details("plain-agent", labels={"user_created": "true"})
    agent_manager._handle_observe_event(make_agent_state_event(agent))

    assert _layout_ops(_drain(q)) == []


def test_assist_agent_rediscovery_does_not_reopen(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A re-emitted AGENT_STATE event for an already-seen assist chat does not reopen its tab."""
    agent = _agent_details("assist-xyz", labels={"assist": "true"})
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    # Register only after the first event so the queue captures just the re-delivery.
    q = broadcaster.register()
    agent_manager._handle_observe_event(make_agent_state_event(agent))

    assert _layout_ops(_drain(q)) == []


def _assist_agent_details(name: str) -> AgentDetails:
    return _agent_details(name, labels={"assist": "true"})


def test_snapshot_auto_opens_a_newly_appeared_assist_chat(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A freshly-created chat usually surfaces in a full snapshot (not a per-agent delta),
    so the snapshot path must auto-open assist chats too."""
    q = broadcaster.register()
    agent = _assist_agent_details("assist-snap")
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))

    messages = _drain(q)
    opens = _layout_ops(messages)
    assert len(opens) == 1
    assert opens[0]["op"] == "open"
    assert opens[0]["args"] == {"ref": "chat:assist-snap"}
    # The agent list must be broadcast before the open, or the frontend drops the open
    # (it resolves ``chat:<name>`` against its known-agents list).
    types = [m.get("type") for m in messages]
    assert types.index("agents_updated") < types.index("layout_op")


def test_snapshot_does_not_reopen_assist_chat_on_later_snapshots(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    agent = _assist_agent_details("assist-snap2")
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))
    # Register after the first snapshot so the queue captures only the second.
    q = broadcaster.register()
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))

    assert _layout_ops(_drain(q)) == []


def test_assist_chat_present_at_startup_is_not_auto_opened(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """Assist chats seeded as already-handled (what ``_initial_discover`` does for chats that
    exist at startup) are not auto-opened, so a restart restores the saved layout."""
    agent = _assist_agent_details("assist-existing")
    with agent_manager._lock:
        agent_manager._auto_opened_assist_ids.add(str(agent.id))
    q = broadcaster.register()
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))

    assert _layout_ops(_drain(q)) == []


def test_agent_removed_event_removes_agent(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """An AGENT_REMOVED event removes the agent from the tracked list and broadcasts."""
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)
    q = broadcaster.register()

    agent = _agent_details("doomed", agent_id=test_agent_id)
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    assert len(agent_manager.get_agents()) == 1

    q.get_nowait()

    agent_manager._handle_observe_event(make_agent_removed_event(agent.id, agent.name, agent.host.id))

    agents = agent_manager.get_agents()
    assert len(agents) == 0

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"
    assert str_id not in [a["id"] for a in msg["agents"]]


def _full_snapshot_with_agent(name: str) -> tuple[MngrAgentId, HostId, AgentDetails]:
    agent = _agent_details(name)
    return agent.id, agent.host.id, agent


def test_full_snapshot_populates_agent_locations(agent_manager: AgentManager) -> None:
    """A snapshot records each agent's routing location (id/host/provider) so messaging skips discovery."""
    agent_id, host_id, agent = _full_snapshot_with_agent("locatable")
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))

    matches = agent_manager.get_agent_matches_by_id(str(agent_id))
    assert len(matches) == 1
    match = matches[0]
    assert str(match.agent_id) == str(agent_id)
    assert str(match.agent_name) == "locatable"
    assert str(match.host_id) == str(host_id)
    assert str(match.provider_name) == "local"

    assert agent_manager.get_agent_matches_by_id("agent-does-not-exist") == []


def test_agent_location_dropped_when_absent_from_snapshot(agent_manager: AgentManager) -> None:
    """An agent missing from a later snapshot loses its cached location."""
    agent_id, _host_id, agent = _full_snapshot_with_agent("ephemeral")
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))
    assert len(agent_manager.get_agent_matches_by_id(str(agent_id))) == 1

    agent_manager._handle_observe_event(make_full_agent_state_event([]))
    assert agent_manager.get_agent_matches_by_id(str(agent_id)) == []


def test_get_agent_matches_by_id_disambiguates_shared_name(agent_manager: AgentManager) -> None:
    """Two agents sharing a name on different hosts are each retrievable by their own id."""
    host_a, host_b = HostId(), HostId()
    agent_a = _agent_details("twin", host_id=host_a)
    agent_b = _agent_details("twin", host_id=host_b)
    agent_manager._handle_observe_event(make_full_agent_state_event([agent_a, agent_b]))

    matches_a = agent_manager.get_agent_matches_by_id(str(agent_a.id))
    matches_b = agent_manager.get_agent_matches_by_id(str(agent_b.id))
    assert len(matches_a) == 1 and str(matches_a[0].host_id) == str(host_a)
    assert len(matches_b) == 1 and str(matches_b[0].host_id) == str(host_b)


def test_agent_location_updates_when_host_changes(agent_manager: AgentManager) -> None:
    """A later snapshot relocating an agent (new host_id) replaces its cached location."""
    agent_id = MngrAgentId()
    host_a, host_b = HostId(), HostId()
    agent_manager._handle_observe_event(
        make_full_agent_state_event([_agent_details("mover", agent_id=agent_id, host_id=host_a)])
    )
    assert str(agent_manager.get_agent_matches_by_id(str(agent_id))[0].host_id) == str(host_a)

    agent_manager._handle_observe_event(
        make_full_agent_state_event([_agent_details("mover", agent_id=agent_id, host_id=host_b)])
    )
    matches = agent_manager.get_agent_matches_by_id(str(agent_id))
    assert len(matches) == 1
    assert str(matches[0].host_id) == str(host_b)


def test_remove_agent_drops_location(agent_manager: AgentManager) -> None:
    """remove_agent (the API destroy path) drops the cached location too."""
    agent_id, _host_id, agent = _full_snapshot_with_agent("doomed")
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))
    assert len(agent_manager.get_agent_matches_by_id(str(agent_id))) == 1

    agent_manager.remove_agent(str(agent_id))
    assert agent_manager.get_agent_matches_by_id(str(agent_id)) == []


def test_get_agent_info_by_id_resolves_from_state(agent_manager: AgentManager, tmp_path: Path) -> None:
    """get_agent_info_by_id builds an AgentInfo from the live state (with resolved dirs)."""
    with agent_manager._lock:
        agent_manager._agents["agent-1"] = AgentStateItem(
            id="agent-1", name="alpha", state="RUNNING", labels={"k": "v"}, work_dir="/w"
        )

    info = agent_manager.get_agent_info_by_id("agent-1")
    assert info is not None
    assert info.id == "agent-1"
    assert info.name == "alpha"
    assert info.labels == {"k": "v"}
    assert agent_manager.get_agent_info_by_id("missing") is None


def test_agent_state_event_locates_agent_immediately(agent_manager: AgentManager) -> None:
    """An AGENT_STATE event records the routing location (id/host/provider) at once,
    so the first message to a just-created agent skips discovery instead of waiting for
    the next full snapshot."""
    fresh = _agent_details("freshly-created")
    agent_manager._handle_observe_event(make_agent_state_event(fresh))

    matches = agent_manager.get_agent_matches_by_id(str(fresh.id))
    assert len(matches) == 1
    assert str(matches[0].agent_name) == "freshly-created"
    assert str(matches[0].host_id) == str(fresh.host.id)
    assert str(matches[0].provider_name) == "local"


def test_on_apps_changed(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path) -> None:
    """Application changes are detected and broadcast."""
    q = broadcaster.register()

    toml_path = tmp_path / "data" / ".state" / "apps.toml"
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text('[[apps]]\nname = "web"\nurl = "http://localhost:8000"\n')

    with agent_manager._lock:
        agent_manager._agents["app-agent"] = AgentStateItem(
            id="app-agent",
            name="app-agent",
            state="RUNNING",
            labels={},
            work_dir=str(tmp_path),
        )

    agent_manager._on_apps_changed("app-agent")

    apps = agent_manager.get_apps()
    assert len(apps) == 1
    assert apps[0].name == "web"

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "apps_updated"


def test_read_apps_handles_invalid_toml(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Invalid TOML files are handled gracefully."""
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text("this is [[ not valid toml {{")

    agent_manager._read_apps(toml_file)

    apps = agent_manager.get_apps()
    assert apps == []


def test_unknown_observe_event_type_is_ignored(agent_manager: AgentManager) -> None:
    """An observe line whose ``type`` is not one of the three agents-stream events is ignored.

    ``parse_observe_event_line`` returns None for unrecognized (forward-compatible)
    types, so the output-line handler must swallow it without raising or mutating
    the tracked agent set.
    """
    line = json.dumps(
        {
            "type": "AGENT_STATE_CHANGE",
            "timestamp": "2026-01-01T00:00:00.000000000Z",
            "event_id": "test-event-id",
            "source": "mngr/agent_states",
        }
    )
    agent_manager._handle_observe_output_line(line, True)
    assert agent_manager.get_agents() == []


def test_create_chat_raises_when_the_primary_work_dir_is_unknown(agent_manager: AgentManager) -> None:
    """A chat has nowhere to be created if the primary's work dir cannot be resolved.

    Both the registered agent and the own-work-dir fallback must be absent for the
    guard to bite, so clear the fallback the fixture provides.
    """
    with agent_manager._lock:
        agent_manager._agents.pop(agent_manager._own_agent_id, None)
        # Empty is the unset form: it is what the manager starts with when
        # MNGR_AGENT_WORK_DIR is absent, and the fallback treats it as falsy.
        agent_manager._own_work_dir = ""
    with pytest.raises(AgentCreationError, match="Cannot determine work directory"):
        agent_manager.create_chat_agent("test")


@pytest.mark.flaky
def test_start_app_watcher(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Starting an app watcher for an agent creates the runtime directory."""
    runtime_dir = tmp_path / "data" / ".state"
    agent_manager._start_app_watcher("watcher-test", tmp_path)
    assert runtime_dir.exists()
    agent_manager._stop_app_watcher("watcher-test")


def test_apps_file_handler_fires_on_move(tmp_path: Path) -> None:
    """The apps-registry watcher must react to move/rename events, not just
    modify events. system/scripts/forward_port.py writes apps.toml atomically
    via ``tempfile.mkstemp`` + ``os.replace``, which surfaces as an
    ``IN_MOVED_TO`` / ``FileMovedEvent`` in watchdog -- if the handler only
    listened on ``on_modified`` every service registration after startup
    would be silently dropped.
    """
    seen: list[str] = []
    handler = _make_apps_file_handler("agent-x", lambda aid: seen.append(aid))

    # Simulate what os.replace(tmp, apps.toml) surfaces as.
    handler.dispatch(
        FileMovedEvent(
            src_path=str(tmp_path / "apps.toml.tmp"),
            dest_path=str(tmp_path / "apps.toml"),
        )
    )

    assert seen == ["agent-x"]


def test_apps_file_handler_ignores_unrelated_paths(tmp_path: Path) -> None:
    """The handler must not fire for writes to forward_port.py's scratch
    ``apps.toml.*.tmp`` files. Every upsert creates and modifies one
    of those before the atomic rename, and firing on each would produce a
    broadcast storm with no useful information (the scratch file is never
    the source of truth we read).
    """
    seen: list[str] = []
    handler = _make_apps_file_handler("agent-x", lambda aid: seen.append(aid))

    handler.dispatch(FileModifiedEvent(src_path=str(tmp_path / "apps.toml.abc123.tmp")))

    assert seen == []


def test_apps_file_handler_ignores_open_and_close_no_write(tmp_path: Path) -> None:
    """The handler must not fire on read-only events (FileOpenedEvent /
    FileClosedNoWriteEvent). Watchdog 3+ emits these on Linux for any open()
    / close() of the watched file -- including the read() inside
    _read_apps itself. If the handler reacts to them it triggers an
    inotify feedback loop that pins one CPU core per agent watcher.
    """
    seen: list[str] = []
    handler = _make_apps_file_handler("agent-x", lambda aid: seen.append(aid))

    handler.dispatch(FileOpenedEvent(src_path=str(tmp_path / "apps.toml")))
    handler.dispatch(FileClosedNoWriteEvent(src_path=str(tmp_path / "apps.toml")))

    assert seen == []


def test_apps_file_handler_fires_on_modify(tmp_path: Path) -> None:
    """A direct write (e.g. ``echo ... > apps.toml``) surfaces as a
    FileModifiedEvent and must still trigger the change callback.
    """
    seen: list[str] = []
    handler = _make_apps_file_handler("agent-x", lambda aid: seen.append(aid))

    handler.dispatch(FileModifiedEvent(src_path=str(tmp_path / "apps.toml")))

    assert seen == ["agent-x"]


def test_stop_app_watcher_nonexistent(agent_manager: AgentManager) -> None:
    """Stopping a watcher for an agent that isn't watched is safe."""
    agent_manager._stop_app_watcher("nonexistent")


def test_initial_discover_populates_agents(
    broadcaster: WebSocketBroadcaster,
) -> None:
    """Initial discovery populates agent list when discovery succeeds."""
    manager = AgentManager.build(broadcaster)
    manager._initial_discover()


def test_initial_discover_handles_errors(
    broadcaster: WebSocketBroadcaster,
) -> None:
    """Initial discovery handles errors gracefully when mngr is unavailable."""
    manager = AgentManager.build(broadcaster)
    manager._initial_discover()
    assert isinstance(manager.get_agents(), list)


def test_refresh_agents_does_not_crash(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """Refresh agents handles errors gracefully and does not raise."""
    agent_manager._refresh_agents()
    assert isinstance(agent_manager.get_agents(), list)


def test_full_snapshot_replaces_agent_set(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """A full state snapshot replaces the entire tracked agent set."""
    q = broadcaster.register()

    agent1 = _agent_details("agent-one", work_dir="/tmp/w1")
    agent2 = _agent_details("agent-two", work_dir="/tmp/w2")
    event = make_full_agent_state_event([agent1, agent2])

    agent_manager._handle_observe_event(event)

    agents = agent_manager.get_agents()
    assert len(agents) == 2

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"
    assert len(msg["agents"]) == 2


def test_run_creation_logs_header_and_completion(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Creation thread logs a header line and a done message."""
    log_q: queue.Queue[str | None] = queue.Queue(maxsize=10000)
    cmd = ["true"]

    done_event = threading.Event()

    def run_and_signal() -> None:
        agent_manager._run_creation("test-id", "test-agent", cmd, tmp_path, log_q, {}, HarnessType.CLAUDE)
        done_event.set()

    t = threading.Thread(target=run_and_signal, daemon=True)
    t.start()
    done_event.wait(timeout=10)

    messages = [json.loads(item) for item in iter(log_q.get_nowait, None)]

    assert any("line" in m and str(tmp_path) in m["line"] for m in messages)
    done_msgs = [m for m in messages if "done" in m]
    assert len(done_msgs) == 1
    assert done_msgs[0]["success"] is True


def test_log_queue_callback_puts_json_line(
    agent_manager: AgentManager,
) -> None:
    """_LogQueueCallback writes each line as a JSON object to the queue."""
    q: queue.Queue[str | None] = queue.Queue()
    cb = _LogQueueCallback(log_queue=q)
    cb("hello\n", True)

    item = q.get_nowait()
    assert item is not None
    assert json.loads(item) == {"line": "hello"}


def test_handle_observe_output_line_empty_is_ignored(agent_manager: AgentManager) -> None:
    """Empty lines from the observe subprocess are silently ignored."""
    agent_manager._handle_observe_output_line("   ", True)
    assert agent_manager.get_agents() == []


def test_handle_observe_output_line_raises_on_invalid_json(agent_manager: AgentManager) -> None:
    """Invalid JSON on stdout from mngr observe surfaces as JSONDecodeError so the upstream bug is visible."""
    with pytest.raises(json.JSONDecodeError):
        agent_manager._handle_observe_output_line("not json {", True)
    assert agent_manager.get_agents() == []


def test_handle_observe_output_line_dispatches_agent_state(
    agent_manager: AgentManager,
) -> None:
    """Valid AGENT_STATE JSONL lines are parsed and dispatched."""
    test_agent_id = MngrAgentId()
    agent = _agent_details("obs-agent", agent_id=test_agent_id)
    event = make_agent_state_event(agent)
    line = json.dumps(event.model_dump(mode="json"))

    agent_manager._handle_observe_output_line(line, True)

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)


def test_handle_observe_event_dispatches_full_state(
    agent_manager: AgentManager,
) -> None:
    """AGENTS_FULL_STATE events surface every agent they carry."""
    test_agent_id = MngrAgentId()
    agent = _agent_details("snap-agent", agent_id=test_agent_id)
    event = make_full_agent_state_event([agent])
    agent_manager._handle_observe_event(event)

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)


def test_handle_observe_event_dispatches_agent_state(
    agent_manager: AgentManager,
) -> None:
    """AGENT_STATE events upsert the single agent they carry."""
    test_agent_id = MngrAgentId()
    agent = _agent_details("disc-agent", agent_id=test_agent_id)
    event = make_agent_state_event(agent)
    agent_manager._handle_observe_event(event)

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)


def test_handle_observe_event_dispatches_agent_removed(
    agent_manager: AgentManager,
) -> None:
    """AGENT_REMOVED events drop the referenced agent."""
    test_agent_id = MngrAgentId()
    agent = _agent_details("to-destroy", agent_id=test_agent_id)
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    assert len(agent_manager.get_agents()) == 1

    agent_manager._handle_observe_event(make_agent_removed_event(agent.id, agent.name, agent.host.id))
    assert len(agent_manager.get_agents()) == 0


def test_full_snapshot_dropping_agents_removes_them(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A later full snapshot that omits previously-tracked agents drops them and broadcasts.

    There is no host event on the observe stream, so the way a whole host's worth
    of agents disappears is a rebuild snapshot that no longer lists them.
    """
    agent_id_1 = MngrAgentId()
    agent_id_2 = MngrAgentId()

    agents = [_agent_details(f"agent-{str(aid)[:8]}", agent_id=aid) for aid in (agent_id_1, agent_id_2)]
    agent_manager._handle_observe_event(make_full_agent_state_event(agents))
    assert len(agent_manager.get_agents()) == 2

    # Register after seeding so the queue captures only the drop broadcast.
    q = broadcaster.register()
    agent_manager._handle_observe_event(make_full_agent_state_event([]))

    assert len(agent_manager.get_agents()) == 0
    assert agent_manager.get_agent_matches_by_id(str(agent_id_1)) == []
    assert agent_manager.get_agent_matches_by_id(str(agent_id_2)) == []
    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"


def test_full_snapshot_omitting_agent_drops_it(
    agent_manager: AgentManager,
) -> None:
    """A rebuild snapshot that no longer lists a tracked agent drops it from the set."""
    agent_id = MngrAgentId()
    agent = _agent_details("host-agent", agent_id=agent_id)
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))
    assert len(agent_manager.get_agents()) == 1

    agent_manager._handle_observe_event(make_full_agent_state_event([]))
    assert len(agent_manager.get_agents()) == 0


def test_build_observe_command_honors_injected_binary(broadcaster: WebSocketBroadcaster) -> None:
    """The ``mngr_binary`` argument to ``build()`` overrides the default binary path."""
    manager = AgentManager.build(broadcaster, mngr_binary="/path/to/custom-mngr")
    try:
        cmd = manager._build_observe_command()
        assert cmd == ["/path/to/custom-mngr", "observe", "--stream-events"]
    finally:
        manager.stop()


# --- mngr CLI argv contract ---
# These confront each builder's argv with the live ``imbue.mngr.main.cli`` tree,
# so a system/vendor/mngr subcommand/flag rename fails here at merge time rather than
# only surfacing at runtime. See ``mngr_cli_contract`` for the validator.


def test_chat_create_argv_selects_harness_by_type_and_role_by_template() -> None:
    """The harness/role split is the contract: `--type` picks the harness, the lone
    `--template` picks the role.

    The harness rides `--type <harness>` (resolving `[agent_types.<harness>]`
    directly), and the `chat` role template -- which never sets `type` -- cannot
    clobber it.
    """
    argv = _build_chat_create_command(
        mngr_binary="mngr",
        name="demo",
        agent_id="agent-123",
        primary_labels={},
        harness=HarnessType.CLAUDE,
    )
    assert argv[argv.index("--type") + 1] == HarnessType.CLAUDE
    templates = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--template"]
    assert templates == ["chat"]


def test_codex_chat_create_argv_accepted_by_live_cli() -> None:
    """The codex harness reuses the chat role verbatim; only the `--type` differs."""
    argv = _build_chat_create_command(
        mngr_binary="mngr",
        name="demo",
        agent_id="agent-123",
        primary_labels={"project": "proj"},
        harness=HarnessType.CODEX,
    )
    assert_mngr_argv_valid(argv)
    assert argv[argv.index("--type") + 1] == HarnessType.CODEX
    templates = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--template"]
    assert templates == ["chat"]


def test_chat_create_argv_accepted_by_live_cli() -> None:
    argv = _build_chat_create_command(
        mngr_binary="mngr",
        name="demo",
        agent_id="agent-123",
        primary_labels={"workspace": "ws", "project": "proj"},
        harness=HarnessType.CLAUDE,
    )
    assert_mngr_argv_valid(argv)
    # The chat carries user_created so the OOM launch wrapper puts it in the
    # dynamic chat band rather than the least-protected worker/unclassified band.
    assert "user_created=true" in argv


def test_every_harness_launches_through_the_oom_band_wrapper() -> None:
    """Each harness's ``[agent_types.<harness>]`` sends its launch through the OOM band
    wrapper, naming its own binary.

    A harness with no ``command`` runs unbanded: earlyoom then sheds it by raw kernel
    score instead of the user/worker tiering, so it can take a user's chat before a
    worker's build subprocess. That is not loud -- nothing fails, the agent just becomes
    disproportionately likely to be killed -- so it is pinned here rather than left to be
    noticed. Driven off ``HarnessType`` so a newly registered harness fails this until it
    is wired up, which is exactly how codex and pi went unbanded.
    """
    settings = tomllib.loads((Path(__file__).parents[5] / ".mngr" / "settings.toml").read_text())
    agent_types = settings["agent_types"]
    for harness in HarnessType:
        command = agent_types[harness.value].get("command", "")
        assert "oom_priority/bin/agent_oom_launch.py" in command, f"{harness} launches unbanded"
        # The wrapper consumes argv[1] as the binary to exec, so it must actually be there.
        assert command.split()[-1], f"{harness} names the wrapper with no binary to exec"


def test_chat_create_argv_carries_no_launch_settings() -> None:
    """Plain chats launch at the harness defaults: no `-S` overrides at all. Fast
    mode rides only the `first` create template (see .mngr/settings.toml), never
    the argv, so every non-first chat starts at standard speed."""
    argv = _build_chat_create_command(
        mngr_binary="mngr",
        name="demo",
        agent_id="agent-123",
        primary_labels={},
        harness=HarnessType.CLAUDE,
    )
    assert "-S" not in argv
    assert not any("fastMode" in token for token in argv)


def test_chat_create_argv_stacks_extra_role_templates_after_chat() -> None:
    """The `first` launcher stacks its template via extra_role_templates; the
    resulting argv must resolve against the live CLI."""
    argv = _build_chat_create_command(
        mngr_binary="mngr",
        name="demo",
        agent_id="agent-123",
        primary_labels={},
        harness=HarnessType.CODEX,
        extra_role_templates=("first",),
    )
    assert_mngr_argv_valid(argv)
    templates = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--template"]
    assert templates == ["chat", "first"]


# --- the chat's originating project (the mngr ``project`` label) ---
# A chat is an agent, so the project it was created inside rides the label mngr
# already propagates to the agent's children rather than a parallel list. The
# label is where a chat starts out filed, not an owner: membership is
# many-to-many and each view's member list says what that view shows.


def test_chat_project_label_prefers_the_project_the_chat_was_created_in() -> None:
    assert _chat_project_label({"project": "taxes"}, "website-redesign") == "website-redesign"


def test_chat_project_label_inherits_the_primary_agents_project_outside_any_project() -> None:
    assert _chat_project_label({"project": "taxes"}, "") == "taxes"


def test_chat_project_label_is_empty_when_nothing_names_a_project() -> None:
    """A chat filed in no project is fine -- Everything lists every object anyway."""
    assert _chat_project_label({}, "") == ""


def test_chat_create_argv_canonicalizes_the_name_and_labels_the_human_one() -> None:
    """A chat is created under its true name with the typed name as a label.

    Both are sent explicitly so the create works against any vendored mngr,
    including one predating free-form names -- and the pair is what newer mngr
    derives for itself, so its "true name is the canonical form of the display
    name" rule holds either way.
    """
    argv = _build_chat_create_command(
        "mngr",
        "Chat 2",
        "agent-1",
        {},
        HarnessType.CLAUDE,
    )

    assert argv[2] == "Chat-2"
    labels = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--label"]
    assert "display_name=Chat 2" in labels
    assert_mngr_argv_valid(argv)


def test_chat_rename_argv_accepted_by_live_cli() -> None:
    """A rename carries the same name pair a create does: canonical name + typed label.

    The canonical name is what an older vendored mngr accepts, and the typed
    name rides the same atomic write as the rename so no observer sees the
    renamed agent without its ``display_name``.
    """
    argv = _build_chat_rename_command(mngr_binary="mngr", agent_id="agent-123", name="Planning notes")
    assert_mngr_argv_valid(argv)
    assert argv == ["mngr", "rename", "agent-123", "Planning-notes", "--label", "display_name=Planning notes"]


def test_chat_display_label_argv_accepted_by_live_cli() -> None:
    """A display-only rename rewrites the label without renaming anything."""
    argv = _build_chat_display_label_command(mngr_binary="mngr", agent_id="agent-123", name="Chat 2")
    assert_mngr_argv_valid(argv)
    assert argv == ["mngr", "label", "agent-123", "--label", "display_name=Chat 2"]


def _tracked_chat(manager: AgentManager, agent_id: str, name: str, display_name: str | None = None) -> None:
    labels = {} if display_name is None else {"display_name": display_name}
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id, name=name, state="RUNNING", labels=labels, work_dir=None
        )


def test_rename_chat_agent_refuses_a_chat_that_is_still_being_created(
    broadcaster: WebSocketBroadcaster,
) -> None:
    """A create in flight already carries a name; renaming to another would race it.

    Filing the name the chat is *already* being created under is the ordinary
    case and is a no-op here; anything else is refused rather than silently
    diverging from whatever the create ends up writing.
    """
    manager = AgentManager.build(broadcaster)
    try:
        with manager._lock:
            manager._proto_agents["proto-1"] = {"agent_id": "proto-1", "name": "Chat 2"}
        manager.rename_chat_agent("proto-1", "Chat 2")
        with pytest.raises(AgentRenameError):
            manager.rename_chat_agent("proto-1", "Something else")
    finally:
        manager.stop()


def test_rename_chat_agent_leaves_mngr_alone_for_an_untracked_id(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
) -> None:
    """An id belonging to no agent has no mngr name to diverge from.

    The stand-in binary always exits non-zero, so this returning quietly is the
    proof that nothing was run: an actual invocation would have raised.
    """
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        manager.rename_chat_agent("agent-nowhere", "Scratch")
    finally:
        manager.stop()


def test_rename_chat_agent_raises_when_mngr_refuses(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
) -> None:
    """A non-zero ``mngr rename`` is an error, and the agent keeps its old name.

    The caller (the member-title endpoint) turns this into an error response and
    records nothing, so the two names cannot drift apart unnoticed.
    """
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        _tracked_chat(manager, "agent-7", "Chat-2")
        with pytest.raises(AgentRenameError):
            manager.rename_chat_agent("agent-7", "Planning notes")
        still_named = manager.get_agent_by_id("agent-7")
        assert still_named is not None
        assert still_named.name == "Chat-2"
    finally:
        manager.stop()


def test_rename_chat_agent_rejects_a_name_already_held_by_another_chat(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
) -> None:
    """Names collide by canonical form, before mngr is even asked.

    "chat 3" canonicalizes to another agent's true name, so the rename is
    refused with the conflict error the endpoint answers 409 with -- and the
    always-failing stand-in binary proves the check fired first.
    """
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        _tracked_chat(manager, "agent-7", "Chat-2", display_name="Chat 2")
        _tracked_chat(manager, "agent-8", "Chat-3", display_name="Chat 3")
        with pytest.raises(AgentNameConflictError):
            manager.rename_chat_agent("agent-7", "chat 3")
    finally:
        manager.stop()


def test_rename_chat_agent_refuses_the_primary_agent(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
) -> None:
    """The services agent's name belongs to the minds app, not to a chat tab."""
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        with manager._lock:
            manager._agents["agent-1"] = AgentStateItem(
                id="agent-1", name="system-services", state="RUNNING", labels={"is_primary": "true"}, work_dir=None
            )
        with pytest.raises(AgentRenameError):
            manager.rename_chat_agent("agent-1", "My machine")
    finally:
        manager.stop()


def test_rename_chat_agent_rejects_a_name_with_no_usable_characters(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
) -> None:
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        _tracked_chat(manager, "agent-7", "Chat-2")
        with pytest.raises(AgentRenameError):
            manager.rename_chat_agent("agent-7", "!!!")
    finally:
        manager.stop()


def _finished_rename(returncode: int, stderr: str, is_timed_out: bool = False) -> FinishedProcess:
    """A rename subprocess's result, as ``run_local_command_modern_version`` shapes it."""
    return FinishedProcess(
        returncode=returncode,
        stdout="",
        stderr=stderr,
        command=("mngr", "rename"),
        is_timed_out=is_timed_out,
        is_output_already_logged=False,
    )


def test_rename_failure_detail_names_our_own_timeout_rather_than_a_signal_number() -> None:
    """A timed-out rename is our cap, and has to read like one.

    ``run_local_command_modern_version`` SIGTERMs a run that hits its timeout
    and reports the kill as a negative return code, so the old wording turned
    the cap in ``_RENAME_TIMEOUT_SECONDS`` into "rename exited with code -15"
    -- which tells the user neither what went wrong nor whether the name landed.
    """
    detail = _rename_failure_detail(
        ["mngr", "rename", "agent-1", "Docs"], _finished_rename(-signal.SIGTERM, "", is_timed_out=True)
    )
    assert "did not finish within" in detail
    assert "-15" not in detail
    # It must not claim the rename did not happen: the subprocess was stopped
    # partway through work that spans the provider's data and a live tmux session.
    assert "may or may not have been applied" in detail


def test_rename_failure_detail_still_reads_as_a_timeout_when_the_kill_had_to_escalate() -> None:
    # A SIGTERM the process ignored escalates to SIGKILL, but the cause is
    # still the timeout -- which is why the wording keys off ``is_timed_out``
    # rather than off which signal did the stopping.
    detail = _rename_failure_detail(["mngr", "rename"], _finished_rename(-signal.SIGKILL, "", is_timed_out=True))
    assert "did not finish within" in detail


def test_rename_failure_detail_prefers_what_the_command_actually_said() -> None:
    detail = _rename_failure_detail(["mngr", "rename"], _finished_rename(1, "  name already taken  "))
    assert detail == "name already taken"


def test_rename_failure_detail_reports_an_ordinary_exit_code_as_one() -> None:
    detail = _rename_failure_detail(["mngr", "rename"], _finished_rename(2, ""))
    assert detail == "'rename' exited with code 2"


def test_rename_failure_detail_names_a_signal_we_did_not_send() -> None:
    # No ``is_timed_out``, so this SIGTERM was not our cap (the OOM shedder,
    # say) and must not be dressed up as one.
    assert _rename_failure_detail(["mngr", "rename"], _finished_rename(-signal.SIGTERM, "")) == (
        "'rename' was stopped by signal 15"
    )
    assert _rename_failure_detail(["mngr", "rename"], _finished_rename(-signal.SIGKILL, "")) == (
        "'rename' was stopped by signal 9"
    )


def test_create_chat_agent_mints_the_first_free_numbered_name(
    agent_manager: AgentManager,
) -> None:
    """An empty requested name allocates "Chat N" server-side, filling gaps.

    "Chat 1" is held by a live agent's display label and "Chat 3" by a chosen
    member title the endpoint passes through, so the mint lands on "Chat 2" --
    and its canonical form is the agent's name.
    """
    _tracked_chat(agent_manager, "agent-1", "Chat-1", display_name="Chat 1")
    created = agent_manager.create_chat_agent("", extra_taken_names=("Chat 3",))
    agent_manager.stop()

    assert created.display_name == "Chat 2"
    assert created.name == "Chat-2"


def test_create_chat_agent_counts_in_flight_creates_as_taken(
    agent_manager: AgentManager,
) -> None:
    """Two concurrent creates cannot both mint "Chat 1": an in-flight create's
    proto entry blocks the slot. The in-flight create is pinned as a proto
    entry directly, so the test cannot race its background completion."""
    with agent_manager._lock:
        agent_manager._proto_agents["proto-1"] = {"agent_id": "proto-1", "name": "Chat 1"}
    created = agent_manager.create_chat_agent("")
    agent_manager.stop()

    assert created.display_name == "Chat 2"


def test_create_chat_agent_numbers_each_harness_under_its_own_word(
    agent_manager: AgentManager,
    tmp_path: Path,
) -> None:
    """A codex chat is "Codex 1", not "Chat 2": the fleets number independently.

    The harness comes from the bound account, so the codex one is named by signing in
    rather than by asking for it -- which is the point: a caller cannot name a harness
    that disagrees with the credential the chat will actually run on.
    """
    agent_manager._auth_gate = lambda check: None
    # The plain chat is created first, while there is nothing signed in, so it lands on
    # the workspace login as claude. Signing in afterwards is what makes the second one
    # codex -- and note it would also make an unbound THIRD chat codex, since the most
    # recently used account is the default.
    chat = agent_manager.create_chat_agent("")
    codex_account_id, _ = mint_account_dir()
    commit_account(codex_account_id, "openai", "OpenAI")
    codex = agent_manager.create_chat_agent("", account_id=codex_account_id)
    agent_manager.stop()

    assert chat.display_name == "Chat 1"
    assert codex.display_name == "Codex 1"


def test_create_chat_agent_rejects_an_explicit_name_that_collides(
    agent_manager: AgentManager,
) -> None:
    """An explicitly requested name that canonicalizes onto an existing agent's
    true name is refused up front (the endpoint answers 409), not left for the
    background mngr create to fail on."""
    _tracked_chat(agent_manager, "agent-1", "Chat-2", display_name="Chat 2")
    with pytest.raises(AgentNameConflictError):
        agent_manager.create_chat_agent("chat 2")
    agent_manager.stop()


def test_create_chat_agent_registers_the_pre_observe_state_under_the_name_pair(
    broadcaster: WebSocketBroadcaster,
    git_work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The AgentStateItem registered before observe relists carries the same
    canonical name + display_name label the created mngr agent will hold, so
    the UI renders identically before and after the relist.

    ``true`` stands in for a succeeding ``mngr create``; the log queue's
    ``done`` sentinel is the deterministic "creation thread finished" signal.
    """
    monkeypatch.setenv("MNGR_AGENT_ID", "test-agent-id")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(git_work_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    true_binary = shutil.which("true")
    assert true_binary is not None
    manager = AgentManager.build(broadcaster, mngr_binary=true_binary)
    try:
        created = manager.create_chat_agent("My planning chat")
        assert created.name == "My-planning-chat"

        log_q = manager.get_log_queue(created.agent_id)
        assert log_q is not None
        done_message: dict[str, Any] | None = None
        deadline = time.monotonic() + 30.0
        while done_message is None:
            raw = log_q.get(timeout=max(0.1, deadline - time.monotonic()))
            message = None if raw is None else json.loads(raw)
            if message is not None and message.get("done"):
                done_message = message
        assert done_message["success"] is True

        agent = manager.get_agent_by_id(created.agent_id)
        assert agent is not None
        assert agent.name == "My-planning-chat"
        assert agent.labels["display_name"] == "My planning chat"
        assert agent.labels["user_created"] == "true"
    finally:
        manager.stop()


def test_chat_create_argv_labels_the_project_the_chat_was_created_in() -> None:
    argv = _build_chat_create_command(
        mngr_binary="mngr",
        name="demo",
        agent_id="agent-123",
        primary_labels={"workspace": "ws", "project": "taxes"},
        harness=HarnessType.CLAUDE,
        project_id="website-redesign",
    )
    assert "project=website-redesign" in argv
    assert "project=taxes" not in argv
    assert_mngr_argv_valid(argv)


def test_chat_create_argv_omits_the_project_label_when_there_is_no_project() -> None:
    argv = _build_chat_create_command(
        mngr_binary="mngr",
        name="demo",
        agent_id="agent-123",
        primary_labels={"workspace": "ws"},
        harness=HarnessType.CLAUDE,
    )
    assert not any(token.startswith("project=") for token in argv)
    assert_mngr_argv_valid(argv)


def test_serialized_agents_expose_the_project_label(broadcaster: WebSocketBroadcaster) -> None:
    """The workspace reads each chat's originating project off the agent payload."""
    manager = AgentManager.build(broadcaster)
    try:
        with manager._lock:
            for agent_id, labels in (
                ("filed", {"user_created": "true", "project": "taxes"}),
                ("unfiled", {"user_created": "true"}),
            ):
                manager._agents[agent_id] = AgentStateItem(
                    id=agent_id, name=agent_id, state="RUNNING", labels=labels, work_dir=None
                )
        project_by_id = {agent["id"]: agent["project"] for agent in manager.get_agents_serialized()}
        assert project_by_id == {"filed": "taxes", "unfiled": None}
    finally:
        manager.stop()


def test_serialized_agents_expose_the_display_name_label(broadcaster: WebSocketBroadcaster) -> None:
    """The human-readable name mngr holds rides along; ``name`` stays the true mngr name."""
    manager = AgentManager.build(broadcaster)
    try:
        with manager._lock:
            for agent_id, name, labels in (
                ("named", "Chat-1", {"user_created": "true", "display_name": "Chat 1"}),
                ("unnamed", "brave-otter", {"user_created": "true"}),
            ):
                manager._agents[agent_id] = AgentStateItem(
                    id=agent_id, name=name, state="RUNNING", labels=labels, work_dir=None
                )
        by_id = {agent["id"]: agent for agent in manager.get_agents_serialized()}
        assert by_id["named"]["display_name"] == "Chat 1"
        assert by_id["unnamed"]["display_name"] is None
        assert by_id["named"]["name"] == "Chat-1"
        assert by_id["unnamed"]["name"] == "brave-otter"
    finally:
        manager.stop()


def test_get_chat_agent_ids_excludes_workers_and_primary(broadcaster: WebSocketBroadcaster) -> None:
    """Only chats are OOM-managed: workers and the primary keep their launch bands."""
    manager = AgentManager.build(broadcaster)
    try:
        with manager._lock:
            for agent_id, labels in (
                ("chat", {"user_created": "true"}),
                ("worker", {"agent_created": "true"}),
                ("primary", {"is_primary": "true"}),
            ):
                manager._agents[agent_id] = AgentStateItem(
                    id=agent_id, name=agent_id, state="RUNNING", labels=labels, work_dir=None
                )
        assert manager.get_chat_agent_ids() == ["chat"]
    finally:
        manager.stop()


def test_observe_argv_accepted_by_live_cli() -> None:
    argv = _build_observe_command_argv("mngr")
    assert_mngr_argv_valid(argv)
    assert "--stream-events" in argv


def test_resolve_observe_cwd_prefers_existing_work_dir(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``MNGR_AGENT_WORK_DIR`` points at a real directory, observe runs there."""
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    manager = AgentManager.build(broadcaster)
    try:
        assert manager._resolve_observe_cwd() == tmp_path
    finally:
        manager.stop()


def test_resolve_observe_cwd_falls_back_when_work_dir_missing(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``MNGR_AGENT_WORK_DIR`` is set but the path does not exist, use ``$HOME``.

    Guards the fallback that keeps observe runnable in tests that stub the env
    var with a non-existent path (e.g. the shared ``agent_manager`` fixture).
    """
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(missing))
    manager = AgentManager.build(broadcaster)
    try:
        assert manager._resolve_observe_cwd() == Path.home()
    finally:
        manager.stop()


def test_resolve_observe_cwd_falls_back_when_work_dir_unset(
    broadcaster: WebSocketBroadcaster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``MNGR_AGENT_WORK_DIR`` unset, observe runs from ``$HOME``."""
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    manager = AgentManager.build(broadcaster)
    try:
        assert manager._resolve_observe_cwd() == Path.home()
    finally:
        manager.stop()


def test_start_observe_spawns_long_lived_subprocess(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the observe subprocess stays alive after startup.

    A healthy ``mngr observe`` keeps running until it is explicitly stopped;
    this test asserts that after ``_start_observe`` returns, the child is
    still running a short window later rather than having exited on its own.
    """
    if shutil.which("mngr") is None:
        pytest.skip("mngr binary not on PATH")

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    # Point the subprocess at a clean cwd with no project-local .mngr/settings.toml;
    # otherwise running pytest from inside a mngr-managed worktree would inherit
    # a config with ``is_allowed_in_pytest = false`` and the child would abort.
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    # And at an empty host dir: with the developer's real ~/.mngr, the spawned
    # observe enumerates their live agents and queries tmux about them, which
    # trips the tmux resource guard on any machine with running agents. The
    # test only asserts the child stays alive, which an empty world satisfies.
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "mngr-host"))
    manager = AgentManager.build(broadcaster)
    try:
        manager._start_observe()
        assert manager._observe_process is not None
        # If the subprocess exits within the window it's a failure (bad command,
        # crashed on startup, etc.). A healthy observe keeps running.
        exited = poll_until(
            lambda: manager._observe_process is not None and manager._observe_process.poll() is not None,
            timeout=1.5,
            poll_interval=0.1,
        )
        assert not exited, (
            "mngr observe subprocess exited within 1.5s of startup "
            f"(returncode={manager._observe_process.returncode}); stderr: "
            f"{manager._observe_process.read_stderr()!r}"
        )
    finally:
        manager.stop()


def test_start_observe_logs_error_when_subprocess_exits_unexpectedly(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
    loguru_records: list[str],
) -> None:
    """If the observe subprocess exits on its own, the watchdog logs an ERROR.

    Uses ``/usr/bin/false`` (or equivalent) as a stand-in mngr binary so the
    spawned process exits immediately with a non-zero code.
    """
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        manager._start_observe()
        logged_error = poll_until(
            lambda: any(r.startswith("ERROR") and "mngr observe" in r for r in loguru_records),
            timeout=5.0,
            poll_interval=0.05,
        )
        assert logged_error, (
            "Expected an ERROR log from the observe watchdog; got: "
            f"{[r for r in loguru_records if r.startswith('ERROR')]}"
        )
    finally:
        manager.stop()


def test_start_observe_watchdog_stays_quiet_on_clean_shutdown(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[str],
) -> None:
    """Calling ``stop()`` on a healthy observe subprocess must not produce errors."""
    if shutil.which("mngr") is None:
        pytest.skip("mngr binary not on PATH")

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    # See test_start_observe_spawns_long_lived_subprocess for why these are needed.
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "mngr-host"))
    manager = AgentManager.build(broadcaster)
    manager._start_observe()
    # ``_start_observe`` only returns after ``run_process_in_background``
    # has spawned the child and its RunningProcess thread has started, so the
    # subprocess is guaranteed to be running by the time we call stop().
    assert manager._observe_process is not None
    manager.stop()

    errors = [r for r in loguru_records if r.startswith("ERROR") and "mngr observe" in r]
    assert errors == [], f"Watchdog logged errors during clean shutdown: {errors}"


def test_handle_observe_output_line_logs_stderr_as_warning(
    agent_manager: AgentManager,
    loguru_records: list[str],
) -> None:
    """Stderr output from the observe subprocess is surfaced as a warning."""
    agent_manager._handle_observe_output_line("something bad happened", is_stdout=False)

    warnings = [r for r in loguru_records if r.startswith("WARNING") and "mngr observe stderr" in r]
    assert warnings, f"Expected a stderr warning; got: {loguru_records}"
    assert "something bad happened" in warnings[0]


# ---------------------------------------------------------------------------
# Activity-state integration
# ---------------------------------------------------------------------------


def test_ensure_activity_tracking_skips_when_state_dir_missing(agent_manager: AgentManager) -> None:
    """No activity tracking is started for an agent whose host_dir state directory is absent."""
    _seed_agent(agent_manager, "remote-agent")
    agent_manager._ensure_activity_tracking("remote-agent")
    try:
        with agent_manager._lock:
            assert "remote-agent" not in agent_manager._activity_tracked_agents
    finally:
        agent_manager.stop()


def test_ensure_activity_tracking_seeds_idle_state_silently(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """When the state dir exists, the agent is seeded as IDLE without broadcasting."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")

    listener = broadcaster.register()
    try:
        agent_manager._ensure_activity_tracking("agent-1")
        # No broadcast should have happened (lifecycle handlers broadcast separately).
        with pytest.raises(queue.Empty):
            listener.get_nowait()

        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
            assert agent_manager._agents["agent-1"].activity_state == ActivityState.IDLE.value
    finally:
        agent_manager.stop()


def test_waiting_lifecycle_trusts_the_active_marker(agent_manager: AgentManager, tmp_path: Path) -> None:
    """The recompute's own wiring: the tracker-declared turn marker is statted and fed to
    derive, so a WAITING agent with a live `active` marker reads THINKING (the observe
    stream can miss a short turn; the marker flips promptly) and settles once it clears."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1", state="WAITING")
    agent_manager._ensure_activity_tracking("agent-1")
    agent_manager.update_session_events(
        "agent-1", [{"type": "user_message", "timestamp": "2026-07-28T00:00:00Z", "content": "go"}]
    )
    assert agent_manager._activity_state_by_agent.get("agent-1") == ActivityState.IDLE

    (state_dir / "active").touch()
    agent_manager._recompute_activity_state("agent-1", broadcast_on_change=False)
    assert agent_manager._activity_state_by_agent.get("agent-1") == ActivityState.THINKING

    (state_dir / "active").unlink()
    agent_manager._recompute_activity_state("agent-1", broadcast_on_change=False)
    assert agent_manager._activity_state_by_agent.get("agent-1") == ActivityState.IDLE


def test_session_events_user_message_drives_thinking(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A user_message at the tail of the transcript flips activity_state to THINKING.

    Replaces the old behavior where THINKING was driven by a transient ``active``
    marker file -- that marker could leak past the end of a turn and falsely
    pin the indicator on "Thinking...". Transcript content is now authoritative.
    """
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    listener = broadcaster.register()
    try:
        agent_manager.update_session_events(
            "agent-1",
            [{"type": "user_message", "content": "go"}],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING
        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        assert agents[0]["activity_state"] == ActivityState.THINKING.value
    finally:
        agent_manager.stop()


def test_session_events_assistant_message_at_tail_is_idle(agent_manager: AgentManager, tmp_path: Path) -> None:
    """An assistant_message with no pending tools at the tail means IDLE."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    try:
        agent_manager.update_session_events(
            "agent-1",
            [
                {"type": "user_message", "content": "go"},
                {"type": "assistant_message", "tool_calls": []},
            ],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
    finally:
        agent_manager.stop()


def test_update_session_events_flips_to_tool_running(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    listener = broadcaster.register()
    try:
        events_with_pending: list[dict[str, Any]] = [
            {
                "type": "assistant_message",
                "tool_calls": [{"tool_call_id": "call_a", "tool_name": "Bash"}],
            }
        ]
        agent_manager.update_session_events("agent-1", events_with_pending)

        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.TOOL_RUNNING

        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        assert agents[0]["activity_state"] == ActivityState.TOOL_RUNNING.value

        # Once the result lands, we flip to THINKING (last event is tool_result,
        # no pending tool_use remains).
        events_resolved = events_with_pending + [{"type": "tool_result", "tool_call_id": "call_a"}]
        agent_manager.update_session_events("agent-1", events_resolved)
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING
    finally:
        agent_manager.stop()


def test_update_session_events_no_op_when_not_tracked(agent_manager: AgentManager) -> None:
    """Calling update_session_events for an untracked agent is a quiet no-op.

    Beyond not raising, it must leave no residue in the per-agent caches:
    otherwise those entries would never be cleared (``_stop_activity_tracking``
    only fires for agents that were being tracked), accumulating indefinitely.
    """
    agent_manager.update_session_events(
        "ghost",
        [{"type": "assistant_message", "tool_calls": [{"tool_call_id": "x", "tool_name": "Bash"}]}],
    )
    with agent_manager._lock:
        assert "ghost" not in agent_manager._activity_state_by_agent
        assert "ghost" not in agent_manager._activity_tracker_by_agent


def test_reset_activity_state_clears_tool_running(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """reset_activity_state flips a stuck TOOL_RUNNING agent back to IDLE and broadcasts.

    Models the interrupt flow: the agent has an unmatched tool_use in its
    transcript (TOOL_RUNNING), then gets restarted. The restart leaves the
    transcript mid-turn, so without an explicit reset the indicator would
    stay pinned at TOOL_RUNNING.
    """
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    listener = broadcaster.register()
    try:
        agent_manager.update_session_events(
            "agent-1",
            [{"type": "assistant_message", "tool_calls": [{"tool_call_id": "call_a", "tool_name": "Bash"}]}],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.TOOL_RUNNING

        agent_manager.reset_activity_state("agent-1")

        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
            assert agent_manager._agents["agent-1"].activity_state == ActivityState.IDLE.value

        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        assert agents[0]["activity_state"] == ActivityState.IDLE.value
    finally:
        agent_manager.stop()


def test_reset_activity_state_no_op_when_not_tracked(agent_manager: AgentManager) -> None:
    """reset_activity_state for an untracked agent is a quiet no-op with no cache residue."""
    agent_manager.reset_activity_state("ghost")
    with agent_manager._lock:
        assert "ghost" not in agent_manager._activity_state_by_agent
        assert "ghost" not in agent_manager._activity_tracker_by_agent


def test_stale_transcript_tail_after_restart_shows_idle(agent_manager: AgentManager, tmp_path: Path) -> None:
    """A running agent whose mid-turn transcript predates the current Claude
    process is shown IDLE, not "Thinking...".

    Reproduces the container-restart case: the transcript still ends on a
    tool_result from the turn that was abandoned when the restart killed Claude,
    so the running-but-idle agent would otherwise stay pinned at THINKING. Once
    mngr touches ``claude_process_started`` on resume, its newer mtime marks the
    tail as stale and the indicator settles on IDLE.
    """
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    # The transcript ends on a tool_result from the distant past (the abandoned
    # turn): an assistant tool_use matched by its tool_result, nothing after.
    agent_manager.update_session_events(
        "agent-1",
        [
            {
                "type": "assistant_message",
                "tool_calls": [{"tool_call_id": "call_a", "tool_name": "Bash"}],
                "timestamp": "2020-01-01T00:00:00.000Z",
            },
            {"type": "tool_result", "tool_call_id": "call_a", "timestamp": "2020-01-01T00:00:01.000Z"},
        ],
    )

    # Before the restart marker exists, the mid-turn tail still reads as THINKING.
    with agent_manager._lock:
        assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING

    # mngr touches claude_process_started on resume; its mtime ("now") is well
    # after the 2020 transcript events.
    (state_dir / "claude_process_started").touch()

    # In production the post-restart observe snapshot drives this recompute.
    agent_manager._recompute_activity_state("agent-1", broadcast_on_change=False)

    with agent_manager._lock:
        assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
        assert agent_manager._agents["agent-1"].activity_state == ActivityState.IDLE.value


def test_codex_agent_gets_a_transcript_turn_latch_tracker(agent_manager: AgentManager, tmp_path: Path) -> None:
    """codex builds a transcript-derived tracker like claude/pi, but its dot is a latch on the
    transcript's real-time turn markers -- NOT the (laggy/unreliable) mngr lifecycle. So a RUNNING
    lifecycle with no open turn in the transcript reads IDLE, not THINKING. Its ledger stays for the
    queue; the daemon-less connection attempt here is a graceful no-op."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX, state="RUNNING")
    agent_manager._ensure_activity_tracking("agent-1")
    with agent_manager._lock:
        assert "agent-1" in agent_manager._activity_tracked_agents
        assert isinstance(agent_manager._activity_tracker_by_agent.get("agent-1"), CodexActivityTracker)
    # RUNNING lifecycle but no turn marker observed -> IDLE (the dot follows the transcript, not mngr).
    assert agent_manager._activity_state_by_agent.get("agent-1") == ActivityState.IDLE
    # A real-time turn_started marker lights it to THINKING.
    agent_manager.update_session_events(
        "agent-1", [{"type": SPECIAL_EVENT_TYPE, "kind": SpecialEventKind.TURN_STARTED.value}]
    )
    assert agent_manager._activity_state_by_agent.get("agent-1") == ActivityState.THINKING
    # No daemon in the test, so the session has no live connection to tap through.
    assert agent_manager._session_by_agent["agent-1"].is_tap_available(has_queued=True) is False


def test_stop_activity_tracking_clears_caches(agent_manager: AgentManager, tmp_path: Path) -> None:
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")
    # Seed a non-default cached state so we can verify it's cleared.
    agent_manager.update_session_events(
        "agent-1",
        [{"type": "user_message", "content": "go"}],
    )

    with agent_manager._lock:
        assert "agent-1" in agent_manager._activity_tracked_agents
        assert "agent-1" in agent_manager._activity_state_by_agent
        assert "agent-1" in agent_manager._activity_tracker_by_agent

    agent_manager._stop_activity_tracking("agent-1")

    with agent_manager._lock:
        assert "agent-1" not in agent_manager._activity_tracked_agents
        assert "agent-1" not in agent_manager._activity_state_by_agent
        assert "agent-1" not in agent_manager._activity_tracker_by_agent


def test_update_queued_messages_caches_broadcasts_and_serializes(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A fresh queued snapshot from the watcher is cached, broadcast, and serialized."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    listener = broadcaster.register()
    try:
        snapshot = [
            {"queued_id": "q1", "content": "hello", "timestamp": "2026-08-07T00:00:01.000Z", "is_sending": False}
        ]
        agent_manager.update_queued_messages("agent-1", snapshot)

        with agent_manager._lock:
            assert agent_manager._agents["agent-1"].queued_messages == (
                QueuedMessageState(queued_id="q1", content="hello", timestamp="2026-08-07T00:00:01.000Z"),
            )
        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        assert agents[0]["queued_messages"] == snapshot
        assert agent_manager.get_agents_serialized()[0]["queued_messages"] == snapshot
    finally:
        agent_manager.stop()


def test_shoulder_tap_available_reflects_queue_and_send_in_flight(agent_manager: AgentManager, tmp_path: Path) -> None:
    """The derived ``shoulder_tap_available`` is true iff something is queued AND no send is in
    flight (contract Shoulder-tap), recomputed at each serialize from the two authoritative
    pieces of manager state -- never stored, so it cannot go stale."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    def available() -> bool:
        return bool(agent_manager.get_agents_serialized()[0]["shoulder_tap_available"])

    # Empty queue -> unavailable (nothing to tap).
    assert available() is False

    # Something queued, no send in flight -> available.
    agent_manager.update_queued_messages("agent-1", [{"queued_id": "q1", "content": "hi", "timestamp": "t"}])
    assert available() is True

    # A send in flight greys it, even with a non-empty queue (the manager consults the
    # session's Sending state, which the session's own send maintains around delivery).
    session = agent_manager._session_by_agent["agent-1"]
    assert isinstance(session, FileHarnessSession)
    session._sending.record("t1", "mid-flight")
    assert available() is False

    # Send resolved -> available again.
    session._sending.resolve("t1")
    assert available() is True

    agent_manager.stop()


def test_update_queued_messages_no_op_when_not_tracked(agent_manager: AgentManager) -> None:
    """Pushing a queued snapshot for an untracked agent leaves no cache residue."""
    agent_manager.update_queued_messages("ghost", [{"queued_id": "q", "content": "x", "timestamp": "t"}])
    with agent_manager._lock:
        assert "ghost" not in agent_manager._queued_messages_by_agent


def test_working_to_idle_drains_the_queue_via_the_registered_handler(
    agent_manager: AgentManager, tmp_path: Path
) -> None:
    """A working->IDLE transition invokes the watcher's queue backstop and clears the group."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    idle_calls: list[bool] = []

    def _drain_handler() -> list[dict[str, Any]]:
        idle_calls.append(True)
        return []

    agent_manager.register_queue_idle_handler("agent-1", _drain_handler)

    try:
        # A queued message is showing while the agent is thinking. The transcript goes
        # THINKING first: a snapshot arriving on an idle agent is swept at arrival by
        # ``update_queued_messages``'s pre-broadcast recompute, and in production the
        # enqueue only ever happens mid-turn.
        agent_manager.update_session_events("agent-1", [{"type": "user_message", "content": "go"}])
        agent_manager.update_queued_messages("agent-1", [{"queued_id": "q1", "content": "hi", "timestamp": "t"}])
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING
            assert len(agent_manager._agents["agent-1"].queued_messages) == 1

        # The turn ends (assistant reply, no pending tools) -> IDLE, so the backstop fires.
        agent_manager.update_session_events(
            "agent-1",
            [{"type": "user_message", "content": "go"}, {"type": "assistant_message", "tool_calls": []}],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
            assert agent_manager._agents["agent-1"].queued_messages == ()
        assert idle_calls == [True]
    finally:
        agent_manager.stop()


def test_idle_agent_with_a_stale_queue_is_swept_without_a_transition(
    agent_manager: AgentManager, tmp_path: Path
) -> None:
    """The backstop is level-triggered: an already-IDLE agent that shows a queued
    survivor is swept on the next recompute, even with no working->IDLE edge.

    The survivor is seeded straight into the caches -- the shape of residue that
    reached the manager with no trigger having run (a snapshot arriving through
    ``update_queued_messages`` is already swept at arrival by its own pre-broadcast
    recompute, covered separately)."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    # _ensure_activity_tracking seeds IDLE with no working->IDLE transition.
    agent_manager._ensure_activity_tracking("agent-1")

    idle_calls: list[bool] = []

    def _drain_handler() -> list[dict[str, Any]]:
        idle_calls.append(True)
        return []

    agent_manager.register_queue_idle_handler("agent-1", _drain_handler)
    try:
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
            # A stale queued entry is showing on the idle agent (no turn in flight).
            stale = (QueuedMessageState(queued_id="q1", content="stale", timestamp="t"),)
            agent_manager._queued_messages_by_agent["agent-1"] = stale
            agent_state = agent_manager._agents["agent-1"]
            agent_manager._agents["agent-1"] = agent_state.model_copy_update(
                to_update(agent_state.field_ref().queued_messages, stale)
            )

        # A plain recompute (agent still IDLE, no edge) must sweep it.
        agent_manager._recompute_activity_state("agent-1", broadcast_on_change=False)
        with agent_manager._lock:
            assert agent_manager._agents["agent-1"].queued_messages == ()
        assert idle_calls == [True]
    finally:
        agent_manager.stop()


def test_queued_snapshot_arriving_while_idle_is_swept_before_broadcast(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A non-empty queued snapshot arriving while the derived state is IDLE (e.g.
    the priming replay resurrecting a dead process's dangling enqueues for a
    stopped agent) triggers the sweep at arrival, and the broadcast carries the
    drained snapshot -- the phantoms are never rendered."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    idle_calls: list[bool] = []

    def _drain_handler() -> list[dict[str, Any]]:
        idle_calls.append(True)
        return []

    agent_manager.register_queue_idle_handler("agent-1", _drain_handler)
    listener = broadcaster.register()
    try:
        agent_manager.update_queued_messages("agent-1", [{"queued_id": "q1", "content": "ghost", "timestamp": "t"}])

        with agent_manager._lock:
            assert agent_manager._agents["agent-1"].queued_messages == ()
        assert idle_calls == [True]
        # The arrival still broadcasts, and no broadcast ever carried the phantom.
        updates = [m for m in _drain(listener) if m.get("type") == "agents_updated"]
        assert updates
        for update in updates:
            assert update["agents"][0]["queued_messages"] == []
    finally:
        agent_manager.stop()


def test_stopped_codex_agent_snapshot_is_swept_before_any_broadcast(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A codex agent whose daemon generation died drops any cached queue chips before broadcast.

    codex's queue is EPHEMERAL and lives with its live ledger; an abrupt daemon kill emits no idle
    sweep, so the dead-lifecycle recompute is what clears the cached chips and settles the dot to
    IDLE. No broadcast ever contains the phantoms.
    """
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX, state="STOPPED")
    agent_manager._ensure_activity_tracking("agent-1")

    try:
        listener = broadcaster.register()
        # The dead generation's orphan chips arrive from a late snapshot push.
        agent_manager.update_queued_messages("agent-1", [{"queued_id": "q1", "content": "phantom", "timestamp": "t"}])

        messages = _drain(listener)
        updates = [message for message in messages if message.get("type") == "agents_updated"]
        assert updates, "the snapshot arrival still broadcasts (the swept state)"
        for update in updates:
            assert update["agents"][0]["queued_messages"] == []
        assert updates[-1]["agents"][0]["activity_state"] == ActivityState.IDLE.value
        with agent_manager._lock:
            assert agent_manager._agents["agent-1"].queued_messages == ()
    finally:
        agent_manager.stop()


def test_queued_snapshot_arriving_mid_turn_is_kept(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """The same snapshot arriving with seeded mid-turn signals (derive non-IDLE)
    is kept: the pre-broadcast sweep only drains an idle agent's queue, so a live
    agent's genuine mirror survives a backend-restart replay."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    idle_calls: list[bool] = []

    def _drain_handler() -> list[dict[str, Any]]:
        idle_calls.append(True)
        return []

    agent_manager.register_queue_idle_handler("agent-1", _drain_handler)
    # Seeded mid-turn signals: a user_message at the tail derives THINKING.
    agent_manager.update_session_events("agent-1", [{"type": "user_message", "content": "go"}])
    listener = broadcaster.register()
    try:
        snapshot = [{"queued_id": "q1", "content": "parked", "timestamp": "t", "is_sending": False}]
        agent_manager.update_queued_messages("agent-1", snapshot)

        with agent_manager._lock:
            assert agent_manager._agents["agent-1"].queued_messages == (
                QueuedMessageState(queued_id="q1", content="parked", timestamp="t"),
            )
        assert idle_calls == []
        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        assert agents[0]["queued_messages"] == snapshot
    finally:
        agent_manager.stop()


def test_unknown_lifecycle_codex_keeps_its_queued_snapshot(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """UNKNOWN is non-evidence (an unreachable provider, not a death): a codex agent's queued snapshot
    survives it -- the queue clear only fires on a positively-dead state. The dot, now lifecycle-driven
    like claude, cannot confirm a live turn under UNKNOWN (codex has no ``active`` marker), so it reads
    IDLE, while the queue is left untouched."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX, state="UNKNOWN")
    agent_manager._ensure_activity_tracking("agent-1")

    try:
        listener = broadcaster.register()
        snapshot = [{"queued_id": "q1", "content": "still parked", "timestamp": "t", "is_sending": False}]
        agent_manager.update_queued_messages("agent-1", snapshot)

        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        assert latest["agents"][0]["queued_messages"] == snapshot
        assert latest["agents"][0]["activity_state"] == ActivityState.IDLE.value
        with agent_manager._lock:
            assert len(agent_manager._agents["agent-1"].queued_messages) == 1
    finally:
        agent_manager.stop()


def test_running_mid_turn_codex_snapshot_passes_through_unchanged(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A codex agent mid-turn (an open turn in the transcript -> THINKING) keeps its queued snapshot:
    a non-dead agent that is working never triggers the idle stale-queue sweep, so the broadcast
    carries the chips."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX, state="RUNNING")
    agent_manager._ensure_activity_tracking("agent-1")

    try:
        # Mid-turn = the transcript's latest marker is turn_started -> the dot latches to THINKING.
        agent_manager.update_session_events(
            "agent-1", [{"type": SPECIAL_EVENT_TYPE, "kind": SpecialEventKind.TURN_STARTED.value}]
        )
        listener = broadcaster.register()
        snapshot = [{"queued_id": "q1", "content": "queued mid-turn", "timestamp": "t", "is_sending": False}]
        agent_manager.update_queued_messages("agent-1", snapshot)

        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        assert latest["agents"][0]["queued_messages"] == snapshot
        assert latest["agents"][0]["activity_state"] == ActivityState.THINKING.value
        with agent_manager._lock:
            assert len(agent_manager._agents["agent-1"].queued_messages) == 1
    finally:
        agent_manager.stop()


def test_stop_activity_tracking_clears_queued_caches(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Stopping tracking drops the queued snapshot and idle handler alongside activity state."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")
    agent_manager.register_queue_idle_handler("agent-1", lambda: [])
    agent_manager.update_queued_messages("agent-1", [{"queued_id": "q1", "content": "hi", "timestamp": "t"}])

    with agent_manager._lock:
        assert "agent-1" in agent_manager._queued_messages_by_agent
        assert "agent-1" in agent_manager._queue_idle_handler_by_agent

    agent_manager._stop_activity_tracking("agent-1")

    with agent_manager._lock:
        assert "agent-1" not in agent_manager._queued_messages_by_agent
        assert "agent-1" not in agent_manager._queue_idle_handler_by_agent


def test_provider_snapshot_preserves_queued_messages_for_tracked_agent(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A re-listing observe snapshot must not wipe an already-tracked agent's queued group."""
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)

    state_dir = tmp_path / "agents" / str_id
    state_dir.mkdir(parents=True)

    agent = _agent_details("snapshot-agent", agent_id=test_agent_id, work_dir=str(tmp_path / "work"))
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    agent_manager.update_queued_messages(str_id, [{"queued_id": "q1", "content": "hi", "timestamp": "t"}])

    listener = broadcaster.register()
    try:
        agent_manager._handle_observe_event(make_full_agent_state_event([agent]))
        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        assert agents[0]["id"] == str_id
        assert agents[0]["queued_messages"] == [
            {"queued_id": "q1", "content": "hi", "timestamp": "t", "is_sending": False}
        ]
    finally:
        agent_manager.stop()


def test_agent_removed_event_fires_removal_side_effects(agent_manager: AgentManager, tmp_path: Path) -> None:
    """An AGENT_REMOVED event drops the agent and clears its activity tracking and caches."""
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)

    state_dir = tmp_path / "agents" / str_id
    state_dir.mkdir(parents=True)
    agent = _agent_details("to-destroy", agent_id=test_agent_id)
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    with agent_manager._lock:
        assert str_id in agent_manager._activity_tracked_agents

    agent_manager._handle_observe_event(make_agent_removed_event(agent.id, agent.name, agent.host.id))

    assert agent_manager.get_agent_by_id(str_id) is None
    with agent_manager._lock:
        assert str_id not in agent_manager._activity_tracked_agents
        assert str_id not in agent_manager._activity_state_by_agent


def test_provider_snapshot_preserves_activity_state_for_tracked_agent(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A per-provider snapshot must not wipe the activity_state of agents that
    are already being tracked for activity.

    Regression test: ``_handle_observe_event`` rebuilds ``_agents`` wholesale
    from the raw observe payload (which has no ``activity_state`` field) on every
    event. Only ids in the membership delta's ``added`` set get an
    ``_ensure_activity_tracking`` recompute, so a snapshot that merely re-lists an
    already-known agent reports it in neither add nor remove. Without re-applying
    the cached state, the broadcast that follows would emit ``activity_state=None``
    for every previously-tracked agent and the chat panel indicator would briefly
    disappear.
    """
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)

    state_dir = tmp_path / "agents" / str_id
    state_dir.mkdir(parents=True)

    # First, simulate the agent already being tracked with a live watcher
    # whose transcript signals THINKING (a user_message with no reply).
    agent = _agent_details("snapshot-agent", agent_id=test_agent_id, work_dir=str(tmp_path / "work"))
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    agent_manager.update_session_events(str_id, [{"type": "user_message", "content": "go"}])
    with agent_manager._lock:
        assert agent_manager._activity_state_by_agent[str_id] == ActivityState.THINKING
        assert agent_manager._agents[str_id].activity_state == ActivityState.THINKING.value

    # Now drain prior broadcasts so the snapshot's broadcast is the only one
    # we read.
    listener = broadcaster.register()
    try:
        snapshot_event = make_full_agent_state_event([agent])
        agent_manager._handle_observe_event(snapshot_event)

        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        # The broadcast must carry the cached activity_state, not None.
        assert agents[0]["id"] == str_id
        assert agents[0]["activity_state"] == ActivityState.THINKING.value

        with agent_manager._lock:
            assert agent_manager._agents[str_id].activity_state == ActivityState.THINKING.value
    finally:
        agent_manager.stop()


def test_agent_state_event_stopped_flips_lifecycle_and_activity_to_idle(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A STOPPED AGENT_STATE event for a tracked, thinking agent broadcasts state=STOPPED
    and re-gates its activity indicator to IDLE.

    The observe stream now carries each agent's real lifecycle state, so an agent
    whose process dies on its own arrives as STOPPED. Because STOPPED is not in
    ``RUNNING_LIFECYCLE_STATES``, the recompute pass must settle its activity to
    IDLE even though the transcript tail still reads THINKING.
    """
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)

    state_dir = tmp_path / "agents" / str_id
    state_dir.mkdir(parents=True)

    running = _agent_details("dying-agent", agent_id=test_agent_id, state=AgentLifecycleState.RUNNING)
    agent_manager._handle_observe_event(make_agent_state_event(running))
    # A pending user_message with no reply pins the transcript-derived state at THINKING.
    agent_manager.update_session_events(str_id, [{"type": "user_message", "content": "go"}])
    with agent_manager._lock:
        assert agent_manager._activity_state_by_agent[str_id] == ActivityState.THINKING

    listener = broadcaster.register()
    try:
        stopped = _agent_details("dying-agent", agent_id=test_agent_id, state=AgentLifecycleState.STOPPED)
        agent_manager._handle_observe_event(make_agent_state_event(stopped))

        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        assert agents[0]["id"] == str_id
        assert agents[0]["state"] == AgentLifecycleState.STOPPED.value
        assert agents[0]["activity_state"] == ActivityState.IDLE.value

        with agent_manager._lock:
            assert agent_manager._agents[str_id].state == AgentLifecycleState.STOPPED.value
            assert agent_manager._activity_state_by_agent[str_id] == ActivityState.IDLE
    finally:
        agent_manager.stop()


def test_full_snapshot_rebuilds_agent_set_and_broadcasts(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A full snapshot rebuilds the tracked set: new agents appear, absent ones are dropped,
    and a single agents_updated broadcast reflects the rebuilt set."""
    first = _agent_details("first-agent")
    agent_manager._handle_observe_event(make_full_agent_state_event([first]))
    assert {a.id for a in agent_manager.get_agents()} == {str(first.id)}

    q = broadcaster.register()
    second = _agent_details("second-agent")
    agent_manager._handle_observe_event(make_full_agent_state_event([second]))

    tracked_ids = {a.id for a in agent_manager.get_agents()}
    assert tracked_ids == {str(second.id)}
    assert str(first.id) not in tracked_ids

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"
    assert {a["id"] for a in msg["agents"]} == {str(second.id)}


# =============================================================================
# Offline codex model-chip resolution from the persisted raw model-list sidecar
# =============================================================================


def _codex_model_entry(model: str, effort: str, *, priority: bool = False) -> CodexModel:
    """A ``model/list`` entry for the sidecar tests (id == model)."""
    return CodexModel.model_validate(
        {
            "id": model,
            "model": model,
            "displayName": model.upper(),
            "supportedReasoningEfforts": [{"reasoningEffort": effort}],
            "serviceTiers": [{"id": "priority"}] if priority else [],
        }
    )


def test_codex_model_options_is_none_without_a_cache_or_a_sidecar(agent_manager: AgentManager) -> None:
    # No in-memory set and no sidecar on disk -> empty (the chip renders nothing).
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX)
    session = agent_manager._build_session("agent-1", HarnessType.CODEX)
    assert session.switch_options() == ()


def test_codex_model_options_falls_back_to_the_sidecar_when_the_cache_is_empty(
    agent_manager: AgentManager,
) -> None:
    # Post-restart: the in-memory set is empty, so the option set is mapped from the persisted raw
    # sidecar -- the whole point of the fix (the chip resolves before the daemon reconnects).
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX)
    models = (_codex_model_entry("gpt-5.6-terra", "high", priority=True),)
    write_codex_model_options(get_codex_model_options_path(agent_manager._get_agent_state_dir("agent-1")), models)
    session = agent_manager._build_session("agent-1", HarnessType.CODEX)
    options = session.switch_options()
    assert [opt.id for opt in options] == ["gpt-5.6-terra"]


def test_codex_model_options_in_memory_cache_wins_over_the_sidecar(agent_manager: AgentManager) -> None:
    # Precedence: a live in-memory set always supersedes the on-disk fallback, so a reconnect's fresh
    # list is authoritative even when a (stale) sidecar exists.
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX)
    stale = (_codex_model_entry("gpt-old", "high"),)
    write_codex_model_options(get_codex_model_options_path(agent_manager._get_agent_state_dir("agent-1")), stale)
    live = codex_models_to_options((_codex_model_entry("gpt-5.6-terra", "high"),))
    session = agent_manager._build_session("agent-1", HarnessType.CODEX)
    session.note_offered_options(live)
    options = session.switch_options()
    assert [opt.id for opt in options] == ["gpt-5.6-terra"]


def test_offline_codex_chip_matches_the_persisted_selection_from_the_sidecar(agent_manager: AgentManager) -> None:
    # The end-to-end offline path: a valid persisted selection plus the raw sidecar (and no live
    # daemon / empty in-memory set) resolves the chip to the real model -- not the "unrecognized
    # model" shrug (matched is None).
    agent_id = "agent-1"
    _seed_agent(agent_manager, agent_id, harness=HarnessType.CODEX)
    state_dir = agent_manager._get_agent_state_dir(agent_id)
    state_path = get_model_state_path(HarnessType.CODEX, state_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"model": "gpt-5.6-terra", "effort": "high", "fast": False}))
    write_codex_model_options(
        get_codex_model_options_path(state_dir), (_codex_model_entry("gpt-5.6-terra", "high", priority=True),)
    )

    # Without the sidecar the identity would match nothing (the pre-fix shrug); with it, the chip resolves.
    agent_manager._session_by_agent[agent_id] = agent_manager._build_session(agent_id, HarnessType.CODEX)
    assert agent_manager._session_by_agent[agent_id].switch_options() != ()
    agent_manager._recompute_model_choice(agent_id, broadcast_on_change=False)
    choice = agent_manager._agents[agent_id].model_choice
    assert choice is not None
    assert choice.identity.model_id == "gpt-5.6-terra"
    assert choice.matched is not None
    assert choice.matched.id == "gpt-5.6-terra"


def _capture_prioritizer_writes(manager: AgentManager, pids: dict[str, int]) -> list[tuple[int, int]]:
    """Swap in an OOM prioritizer that captures its band writes, and return the log.

    Wired to the manager's own ``get_chat_agent_ids`` / ``_read_process_started_at``
    (the collaborators under test) but to a fake pid resolver and a capturing
    ``set_adj``, so the manager's real seeding and lifecycle paths are exercised
    without touching ``/proc``.
    """
    writes: list[tuple[int, int]] = []
    manager._oom_prioritizer = ChatOomPrioritizer(
        list_chat_agent_ids=manager.get_chat_agent_ids,
        resolve_pid=lambda cid: pids.get(cid),
        set_adj=lambda pid, adj: (writes.append((pid, adj)), True)[1],
        resolve_process_started_at=manager._read_agent_process_started_at,
    )
    return writes


def _write_client_activity_message(host_dir: Path, agent_id: str, seconds_ago: float) -> None:
    """Append one message event, stamped ``seconds_ago`` before now, to the activity log.

    Relative to now rather than a fixed date because the band the seeded stamp
    produces depends on how long ago it was: a pinned date drifts out of the
    freshness ramp as wall-clock advances, and every chat then reads as equally
    abandoned.
    """
    timestamp = format_nanosecond_iso_timestamp(datetime.now(timezone.utc) - timedelta(seconds=seconds_ago))
    events_path = client_activity.get_events_path(projects.primary_agent_layout_dir(host_dir, "test-agent-id"))
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a") as event_file:
        event_file.write(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "type": "message",
                    "event_id": f"evt-{agent_id}-{timestamp}",
                    "source": "client_activity",
                    "client_id": "client-1",
                    "device_kind": "desktop",
                    "layout_slug": "desktop",
                    "agent_id": agent_id,
                    "agent_name": agent_id,
                    "message_text": "hi",
                    "is_message_truncated": False,
                }
            )
            + "\n"
        )


def test_seeding_recovers_chat_message_recency_from_the_activity_log(
    agent_manager: AgentManager, tmp_path: Path
) -> None:
    """A restarted system interface recovers which chats were recently messaged.

    The prioritizer's recency state is in-memory, so on restart it is re-seeded
    from the durable client-activity log. Without that, every chat would look
    never-messaged and start aging from its process-start time.
    """
    older = _agent_details("older-chat", labels={"user_created": "true"})
    newer = _agent_details("newer-chat", labels={"user_created": "true"})
    agent_manager._handle_observe_event(make_full_agent_state_event([older, newer]))
    writes = _capture_prioritizer_writes(agent_manager, {str(older.id): 10, str(newer.id): 20})

    _write_client_activity_message(tmp_path, str(older.id), seconds_ago=60 * 60)
    _write_client_activity_message(tmp_path, str(newer.id), seconds_ago=30 * 60)
    agent_manager._seed_oom_prioritizer()
    agent_manager._oom_prioritizer.reapply()

    latest = {pid: adj for pid, adj in writes}
    # The more recently messaged chat outranks the other, which only holds if the
    # log's timestamps were found, parsed, and ordered.
    assert latest[20] < latest[10]


def _age_process_start_marker(manager: AgentManager, agent_id: str, seconds_ago: float) -> None:
    """Backdate the agent's ``claude_process_started`` marker, so it reads as idle."""
    state_dir = manager._get_agent_state_dir(agent_id)
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / "claude_process_started"
    marker.touch()
    old = time.time() - seconds_ago
    os.utime(marker, (old, old))


def test_observe_events_exempt_a_running_chat_from_aging_out(agent_manager: AgentManager) -> None:
    """A chat mid-turn stays below the worker band however long it has been idle.

    The observe stream is the prioritizer's only view of a chat messaged outside
    the workspace UI, so this is what keeps such a chat -- e.g. one running a long
    task another agent kicked off -- from being shed mid-task.
    """
    chat = _agent_details("busy-chat", labels={"user_created": "true"}, state=AgentLifecycleState.RUNNING)
    chat_id = str(chat.id)
    agent_manager._handle_observe_event(make_full_agent_state_event([chat]))
    _age_process_start_marker(agent_manager, chat_id, seconds_ago=3 * 24 * 3600)
    writes = _capture_prioritizer_writes(agent_manager, {chat_id: 10})

    agent_manager._handle_observe_event(make_full_agent_state_event([chat]))
    assert writes, "a running chat should have been re-tagged from the observe event"
    assert writes[-1][1] < bands.WORKER_AGENT

    # The turn ends; the chat is no longer exempt, but the turn's end counts as
    # engagement, so it resumes aging from now rather than from three days ago.
    stopped = _agent_details(
        "busy-chat", agent_id=chat.id, labels={"user_created": "true"}, state=AgentLifecycleState.WAITING
    )
    agent_manager._handle_observe_event(make_full_agent_state_event([stopped]))
    assert writes[-1][1] == bands.CHAT_AGENT_BASE


def test_session_cache_heals_when_the_real_harness_arrives(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Tracking can start before observe reports the agent (the create path), caching a
    DEFAULT-harness session; the first caller that knows the real harness must replace it,
    or a codex agent would send through mngr's file API forever."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    # Tracking starts with no _agents entry -> the claude default guess.
    agent_manager._ensure_activity_tracking("agent-1")
    assert agent_manager._session_by_agent["agent-1"].harness is HarnessType.CLAUDE
    # The observe stream catches up: the agent is codex; re-tracking heals both caches.
    _seed_agent(agent_manager, "agent-1", harness=HarnessType.CODEX)
    agent_manager._ensure_activity_tracking("agent-1")
    assert agent_manager._session_by_agent["agent-1"].harness is HarnessType.CODEX
    assert isinstance(agent_manager._activity_tracker_by_agent["agent-1"], CodexActivityTracker)


def test_stop_activity_tracking_keeps_the_sending_records(agent_manager: AgentManager, tmp_path: Path) -> None:
    """A transient discovery blip quiesces the session without destroying it: an in-flight
    Sending record must survive so a stop after the blip still returns the text (A4) --
    the same lifetime the watcher registry has."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")
    session = agent_manager._session_by_agent["agent-1"]
    assert isinstance(session, FileHarnessSession)
    session._sending.record("t-inflight", "caught mid-send")

    agent_manager._stop_activity_tracking("agent-1")
    assert agent_manager._session_by_agent["agent-1"] is session
    assert session.in_flight_block() == "caught mid-send"
