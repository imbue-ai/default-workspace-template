"""Tests for ``layout_ops`` (mutex, inspect serializer, list helper, op-name validation)."""

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.layout_ops import LayoutMutex
from imbue.system_interface.layout_ops import _service_name_from_url
from imbue.system_interface.layout_ops import _service_session_suffix
from imbue.system_interface.layout_ops import allocate_next_terminal_name
from imbue.system_interface.layout_ops import allocate_terminal_panel_id
from imbue.system_interface.layout_ops import filter_user_terminal_sessions
from imbue.system_interface.layout_ops import is_broadcasting_op
from imbue.system_interface.layout_ops import is_destroyable_terminal_session
from imbue.system_interface.layout_ops import is_known_op
from imbue.system_interface.layout_ops import is_mutating_op
from imbue.system_interface.layout_ops import is_sessionless_browser_ref
from imbue.system_interface.layout_ops import layout_inspect
from imbue.system_interface.layout_ops import layout_list
from imbue.system_interface.layout_ops import parse_tmux_sessions_output
from imbue.system_interface.layout_ops import terminal_origin_label

# A workspace host as the frontend sees it: the ``host-<32hex>`` coordinate
# label plus the local base. Service URLs prefix the service name as one more
# label (``http://web.<_LOCAL_WORKSPACE_HOST>/``).
_LOCAL_WORKSPACE_HOST = "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421"

# The same coordinate on a (future) shared hostname: the nesting rule is
# identical, only the base after ``host-<hex>`` is longer.
_SHARED_WORKSPACE_HOST = "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.us-east.imbueminds.com"

# A workspace-keyed share hostname: the coordinate leads with the bare 32-hex
# share label (no ``host-`` prefix), followed by the one-way user hash. The
# nesting rule is the same -- a service prefixes its label as one more label.
_WORKSPACE_KEYED_SHARED_HOST = (
    "5f13881abca599b0e91695294922fd15.103de49d5bad06cb6892f8c9e68c0cf6.us1.imbueminds.com"
)


def test_is_sessionless_browser_ref() -> None:
    # Bare browser ref (or an empty session) is the orphan-pane case -> rejected.
    assert is_sessionless_browser_ref("service:browser") is True
    assert is_sessionless_browser_ref("service:browser?session=") is True
    assert is_sessionless_browser_ref("service:browser?foo=bar") is True
    # A real session name is fine.
    assert is_sessionless_browser_ref("service:browser?session=alex-smith") is False
    # Not a browser ref (or not a service ref, or non-string) -> not our concern.
    assert is_sessionless_browser_ref("service:web") is False
    assert is_sessionless_browser_ref("service:browserfoo") is False
    assert is_sessionless_browser_ref("chat:alex-smith") is False
    assert is_sessionless_browser_ref(None) is False


def test_known_ops_cover_the_full_surface() -> None:
    for op in (
        "list",
        "inspect",
        "open",
        "focus",
        "split",
        "close",
        "move",
        "rename",
        "maximize",
        "restore",
        "replace-url",
        "refresh",
    ):
        assert is_known_op(op), op


def test_unknown_op_is_rejected() -> None:
    assert not is_known_op("explode")
    assert not is_known_op("")


def test_mutating_ops_include_focus_but_not_refresh_or_queries() -> None:
    """``focus`` mutates serialized active-panel state, so it acquires the mutex."""
    assert is_mutating_op("focus")
    # Pure queries bypass.
    assert not is_mutating_op("list")
    assert not is_mutating_op("inspect")
    # ``refresh`` is state-preserving and bypasses too.
    assert not is_mutating_op("refresh")


def test_broadcasting_ops_exclude_list_and_inspect() -> None:
    """``list`` and ``inspect`` are pure server-side queries; they never broadcast."""
    assert not is_broadcasting_op("list")
    assert not is_broadcasting_op("inspect")
    assert is_broadcasting_op("refresh")
    assert is_broadcasting_op("open")


def test_mutex_acquire_succeeds_when_free() -> None:
    mutex = LayoutMutex(ttl_seconds=0.5)
    assert mutex.try_acquire("agent-a", "move", {"ref": "service:web"}) is None


def test_mutex_acquire_fails_while_held_and_returns_holder_info() -> None:
    mutex = LayoutMutex(ttl_seconds=1.0)
    assert mutex.try_acquire("agent-a", "move", {"ref": "service:web"}) is None

    holder = mutex.try_acquire("agent-b", "split", {"ref": "service:api"})
    assert holder is not None
    assert holder["agent_id"] == "agent-a"
    assert holder["operation"] == "move"
    assert holder["args"] == {"ref": "service:web"}
    assert isinstance(holder["started_at"], float)


def test_mutex_release_allows_subsequent_acquire() -> None:
    mutex = LayoutMutex(ttl_seconds=1.0)
    assert mutex.try_acquire("agent-a", "move", {}) is None
    mutex.release("agent-a", "move")
    assert mutex.try_acquire("agent-b", "split", {}) is None


def test_mutex_release_of_non_holder_is_noop() -> None:
    """A stale release from a previous holder must NOT release a current holder's slot."""
    mutex = LayoutMutex(ttl_seconds=1.0)
    assert mutex.try_acquire("agent-a", "move", {}) is None
    # Stale release from a different agent.
    mutex.release("agent-b", "move")
    holder = mutex.try_acquire("agent-c", "split", {})
    assert holder is not None
    assert holder["agent_id"] == "agent-a"


def test_mutex_auto_releases_after_ttl() -> None:
    mutex = LayoutMutex(ttl_seconds=0.01)
    assert mutex.try_acquire("agent-a", "move", {}) is None
    wait_for(
        lambda: mutex.try_acquire("agent-b", "split", {}) is None,
        timeout=1.0,
        poll_interval=0.005,
        error_message="mutex did not auto-release after TTL",
    )


def _write_layout(path: Path, dockview: dict[str, Any], panel_params: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dockview": dockview, "panelParams": panel_params}))


def test_inspect_returns_empty_when_no_layout_file(tmp_path: Path) -> None:
    summary = layout_inspect(tmp_path / "missing.json", {})
    assert summary == {"active_panel": None, "panels": [], "tree": None}


def test_inspect_resolves_chat_ref_via_agent_name_map(tmp_path: Path) -> None:
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={
            "panels": {"p1": {"id": "p1", "title": "alice-chat"}},
            "grid": {"root": {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 1.0}}},
        },
        panel_params={"p1": {"panelType": "chat", "chatAgentId": "agent-42"}},
    )
    summary = layout_inspect(layout_path, {"agent-42": "alice"})
    refs = [p["ref"] for p in summary["panels"]]
    assert "chat:alice" in refs


def test_inspect_resolves_iframe_with_service_name(tmp_path: Path) -> None:
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={
            "panels": {"p1": {"id": "p1", "title": "web"}},
            "grid": {"root": {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 1.0}}},
        },
        panel_params={"p1": {"panelType": "iframe", "serviceName": "web"}},
    )
    summary = layout_inspect(layout_path, {})
    refs = [p["ref"] for p in summary["panels"]]
    assert "service:web" in refs


def test_inspect_emits_chat_terminal_ref_for_agent_attached_terminal(tmp_path: Path) -> None:
    """An iframe pointed at the per-agent terminal URL projects to ``chat-terminal:<name>``.

    The chat panel's "Open agent terminal" button mints iframes pointed at
    the terminal service's own origin with dispatch args
    (``http://terminal.host-<hex>.localhost:8421/?arg=_&arg=agent&arg=<name>``);
    ``_resolve_ref`` must recognize that URL shape and emit the stable
    ``chat-terminal:<name>`` ref so the panel is addressable by name
    rather than via an opaque ``terminal:<hash>``.
    """
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={
            "panels": {"p1": {"id": "p1", "title": "alice terminal"}},
            "grid": {"root": {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 1.0}}},
        },
        panel_params={
            "p1": {
                "panelType": "iframe",
                "url": f"http://terminal.{_LOCAL_WORKSPACE_HOST}/?arg=_&arg=agent&arg=alice",
            }
        },
    )
    summary = layout_inspect(layout_path, {})
    refs = [p["ref"] for p in summary["panels"]]
    assert "chat-terminal:alice" in refs


def test_inspect_emits_chat_terminal_ref_on_shared_origin(tmp_path: Path) -> None:
    """The shared host shape (``terminal.host-<hex>.<user>.<region>.<domain>``) is recognized too.

    Share hostnames follow the SAME nesting rule as local ones -- the service
    name is prefixed as the first label; only the base after the
    ``host-<hex>`` coordinate is longer -- so the origin-based service
    detection needs no share-specific branch, just this pin.
    """
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={
            "panels": {"p1": {"id": "p1", "title": "alice terminal"}},
            "grid": {"root": {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 1.0}}},
        },
        panel_params={
            "p1": {
                "panelType": "iframe",
                "url": f"https://terminal.{_SHARED_WORKSPACE_HOST}/?arg=_&arg=agent&arg=alice",
            }
        },
    )
    summary = layout_inspect(layout_path, {})
    refs = [p["ref"] for p in summary["panels"]]
    assert "chat-terminal:alice" in refs


def test_inspect_keeps_anonymous_terminal_as_terminal_hash_ref(tmp_path: Path) -> None:
    """Terminals minted by the "New terminal" button use ``arg=workdir`` and stay ``terminal:<hash>``.

    Only the agent-attached terminal pattern (``arg=agent&arg=<name>``)
    projects to ``chat-terminal:<name>``; everything else on the terminal
    service's origin falls back to the opaque hash form.
    """
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={
            "panels": {"p1": {"id": "p1", "title": "terminal"}},
            "grid": {"root": {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 1.0}}},
        },
        panel_params={
            "p1": {
                "panelType": "iframe",
                "url": f"http://terminal.{_LOCAL_WORKSPACE_HOST}/?arg=_&arg=workdir&arg=%2Fmngr%2Fcode",
            }
        },
    )
    summary = layout_inspect(layout_path, {})
    panel = summary["panels"][0]
    assert panel["ref"].startswith("terminal:")
    assert not panel["ref"].startswith("chat-terminal:")


def test_inspect_emits_url_ref_for_ad_hoc_iframe(tmp_path: Path) -> None:
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={
            "panels": {"p1": {"id": "p1", "title": "external"}},
            "grid": {"root": {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 1.0}}},
        },
        panel_params={"p1": {"panelType": "iframe", "url": "https://example.com/"}},
    )
    summary = layout_inspect(layout_path, {})
    panel = summary["panels"][0]
    assert panel["ref"].startswith("url:")
    # The ``url`` field is surfaced on iframe panel summaries so the CLI's
    # wait-stable / no-op detection for ``replace-url`` and ``open
    # https://...`` can match by URL rather than by the synthetic
    # ``url:<hash>`` ref.
    assert panel["url"] == "https://example.com/"


def test_inspect_preserves_grid_arrangement_in_tree(tmp_path: Path) -> None:
    layout_path = tmp_path / "layout.json"
    # Dockview persists the orientation on the *grid* (once) rather than
    # on each branch node, matching ``dockview-core``'s gridview format.
    _write_layout(
        layout_path,
        dockview={
            "panels": {
                "p1": {"id": "p1", "title": "chat"},
                "p2": {"id": "p2", "title": "web"},
            },
            "grid": {
                "orientation": "HORIZONTAL",
                "root": {
                    "type": "branch",
                    "size": 1.0,
                    "data": [
                        {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 0.4}},
                        {"type": "leaf", "data": {"views": ["p2"], "activeView": "p2", "size": 0.6}},
                    ],
                },
            },
        },
        panel_params={
            "p1": {"panelType": "chat", "chatAgentId": "agent-42"},
            "p2": {"panelType": "iframe", "serviceName": "web"},
        },
    )
    summary = layout_inspect(layout_path, {"agent-42": "alice"})
    tree = summary["tree"]
    assert tree["type"] == "branch"
    # Dockview ``HORIZONTAL`` (children side by side) is exposed as
    # ``arrangement: "row"``.
    assert tree["arrangement"] == "row"
    assert len(tree["children"]) == 2
    # Coarse size ratios are preserved so an agent can reason about layout.
    assert tree["children"][0]["size_ratio"] == 0.4
    assert tree["children"][1]["size_ratio"] == 0.6


def test_inspect_alternates_arrangement_at_each_nesting_level(tmp_path: Path) -> None:
    """Nested branches alternate ``row`` / ``column`` as dockview-gridview does.

    Dockview only stores the root grid orientation; child branch nodes
    carry no ``orientation`` field of their own. Reading the field per
    branch (as the serializer used to) defaults nested branches to a
    constant arrangement and reports nested layouts incorrectly. The
    serializer threads the root orientation down and flips it at each
    level of nesting to match dockview's ``orthogonal`` semantics.
    """
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={
            "panels": {
                "p1": {"id": "p1", "title": "a"},
                "p2": {"id": "p2", "title": "b"},
                "p3": {"id": "p3", "title": "c"},
            },
            "grid": {
                "orientation": "VERTICAL",
                "root": {
                    "type": "branch",
                    "size": 1.0,
                    "data": [
                        {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 0.3}},
                        {
                            "type": "branch",
                            "size": 0.7,
                            "data": [
                                {"type": "leaf", "data": {"views": ["p2"], "activeView": "p2", "size": 0.5}},
                                {"type": "leaf", "data": {"views": ["p3"], "activeView": "p3", "size": 0.5}},
                            ],
                        },
                    ],
                },
            },
        },
        panel_params={
            "p1": {"panelType": "chat", "chatAgentId": "agent-a"},
            "p2": {"panelType": "chat", "chatAgentId": "agent-b"},
            "p3": {"panelType": "chat", "chatAgentId": "agent-c"},
        },
    )
    summary = layout_inspect(layout_path, {"agent-a": "a", "agent-b": "b", "agent-c": "c"})
    tree = summary["tree"]
    # Root grid orientation is VERTICAL -> children stack -> column.
    assert tree["arrangement"] == "column"
    # The nested branch is one level down; gridview flips orientation, so
    # its children are arranged side by side -> row.
    nested = tree["children"][1]
    assert nested["type"] == "branch"
    assert nested["arrangement"] == "row"


def test_inspect_defaults_missing_grid_orientation_to_horizontal(tmp_path: Path) -> None:
    """A layout missing ``grid.orientation`` still renders deterministically.

    Older saved layouts (and the empty-dockview boot path) don't always
    write the orientation field. The serializer falls back to
    ``HORIZONTAL`` rather than producing a tree without an ``arrangement``.
    """
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={
            "panels": {"p1": {"id": "p1", "title": "only"}},
            "grid": {
                "root": {
                    "type": "branch",
                    "size": 1.0,
                    "data": [
                        {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 1.0}},
                    ],
                },
            },
        },
        panel_params={"p1": {"panelType": "chat", "chatAgentId": "a"}},
    )
    summary = layout_inspect(layout_path, {"a": "a"})
    assert summary["tree"]["arrangement"] == "row"


def test_list_marks_open_services_via_layout_json(tmp_path: Path) -> None:
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={"panels": {"p1": {"id": "p1", "title": "web"}}},
        panel_params={"p1": {"panelType": "iframe", "serviceName": "web"}},
    )
    entries = layout_list(
        service_names=("web", "api"),
        agents=[],
        layout_json_path=layout_path,
        agent_name_by_id={},
    )
    by_ref = {e["ref"]: e for e in entries}
    assert by_ref["service:web"]["is_open"] is True
    assert by_ref["service:api"]["is_open"] is False


def test_list_hides_reserved_system_interface_entry(tmp_path: Path) -> None:
    """The chrome's own ``system_interface`` registration is filtered server-side
    so every caller (script, direct HTTP, future SDKs) sees the same set."""
    entries = layout_list(
        service_names=("web", "system_interface", "api"),
        agents=[],
        layout_json_path=tmp_path / "missing.json",
        agent_name_by_id={},
    )
    refs = {e["ref"] for e in entries}
    assert "service:web" in refs
    assert "service:api" in refs
    assert "service:system_interface" not in refs


def test_allocate_terminal_panel_id_returns_terminal_ref_for_panel_id() -> None:
    """The frontend uses the supplied panel id verbatim, so the returned
    ref must be derived from that same panel id -- otherwise the script's
    printed ``terminal:<hash>`` won't address the panel that gets created.

    Asserts the shape contract (``iframe-terminal-<id>`` panel id paired
    with a ``terminal:<8 hex chars>`` ref) rather than reimplementing the
    short-hash mapping, which would couple the test to an internal helper.
    """
    panel_id, ref = allocate_terminal_panel_id()
    assert panel_id.startswith("iframe-terminal-")
    assert ref.startswith("terminal:")
    hash_part = ref.removeprefix("terminal:")
    assert len(hash_part) == 8
    assert all(c in "0123456789abcdef" for c in hash_part)
    # Successive allocations must not collide -- otherwise two
    # near-simultaneous ``open terminal`` calls would clobber each other.
    _, ref_again = allocate_terminal_panel_id()
    assert ref_again != ref


def test_allocate_terminal_panel_id_is_unique_per_call() -> None:
    """Each ``open terminal`` / ``split terminal`` mints a fresh tab; the
    server's allocation must never collide across calls or two
    near-simultaneous terminal opens would clobber each other."""
    ids = {allocate_terminal_panel_id()[0] for _ in range(50)}
    assert len(ids) == 50


def test_list_marks_running_agents(tmp_path: Path) -> None:
    entries = layout_list(
        service_names=(),
        agents=[
            {"id": "a1", "name": "alice", "state": "running", "labels": {}, "work_dir": None},
            {"id": "a2", "name": "bob", "state": "stopped", "labels": {}, "work_dir": None},
        ],
        layout_json_path=tmp_path / "missing.json",
        agent_name_by_id={"a1": "alice", "a2": "bob"},
    )
    by_ref = {e["ref"]: e for e in entries}
    assert by_ref["chat:alice"]["is_running"] is True
    assert by_ref["chat:bob"]["is_running"] is False


def test_list_emits_chat_terminal_entry_per_agent(tmp_path: Path) -> None:
    """``layout_list`` exposes the per-agent terminal as a discoverable ref.

    Surfacing ``chat-terminal:<name>`` in ``list`` lets callers see the
    terminal exists before opening it, mirroring how ``chat:<name>``
    advertises the chat tab. ``is_running`` tracks the owning agent so
    a stopped agent's terminal is flagged accordingly.
    """
    entries = layout_list(
        service_names=(),
        agents=[
            {"id": "a1", "name": "alice", "state": "running", "labels": {}, "work_dir": None},
            {"id": "a2", "name": "bob", "state": "stopped", "labels": {}, "work_dir": None},
        ],
        layout_json_path=tmp_path / "missing.json",
        agent_name_by_id={"a1": "alice", "a2": "bob"},
    )
    by_ref = {e["ref"]: e for e in entries}
    assert "chat-terminal:alice" in by_ref
    assert by_ref["chat-terminal:alice"]["kind"] == "agent-terminal"
    assert by_ref["chat-terminal:alice"]["is_running"] is True
    assert by_ref["chat-terminal:alice"]["is_open"] is False
    assert by_ref["chat-terminal:bob"]["is_running"] is False


def test_list_chat_terminal_marks_open_when_url_is_mounted(tmp_path: Path) -> None:
    """``is_open`` on the ``chat-terminal:`` entry tracks the agent-attached URL.

    The ``_collect_open_refs`` helper builds the mount set from the same
    ``_resolve_ref`` projection that ``inspect`` uses, so the listing
    stays in sync with what would appear there.
    """
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={
            "panels": {"p1": {"id": "p1", "title": "alice terminal"}},
            "grid": {"root": {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 1.0}}},
        },
        panel_params={
            "p1": {
                "panelType": "iframe",
                "url": f"http://terminal.{_LOCAL_WORKSPACE_HOST}/?arg=_&arg=agent&arg=alice",
            }
        },
    )
    entries = layout_list(
        service_names=(),
        agents=[
            {"id": "a1", "name": "alice", "state": "running", "labels": {}, "work_dir": None},
            {"id": "a2", "name": "bob", "state": "running", "labels": {}, "work_dir": None},
        ],
        layout_json_path=layout_path,
        agent_name_by_id={"a1": "alice", "a2": "bob"},
    )
    by_ref = {e["ref"]: e for e in entries}
    assert by_ref["chat-terminal:alice"]["is_open"] is True
    assert by_ref["chat-terminal:bob"]["is_open"] is False


def test_parse_tmux_sessions_output_parses_name_id_and_path() -> None:
    output = "terminal-1\t$3\t/home/user/workspace\nmngr-alice\t$1\t/home/user\n"
    sessions = parse_tmux_sessions_output(output)
    assert len(sessions) == 2
    assert sessions[0].session_name == "terminal-1"
    assert sessions[0].session_id == "$3"
    assert sessions[0].cwd == "/home/user/workspace"
    assert sessions[1].session_name == "mngr-alice"


def test_parse_tmux_sessions_output_skips_blank_and_malformed_lines() -> None:
    output = "\nterminal-1\t$3\t/home/user/workspace\nbroken-line-no-tabs\n\t\t\n"
    sessions = parse_tmux_sessions_output(output)
    # The all-empty tab line still has three fields, so only the truly
    # malformed (no-tab) and blank lines are dropped.
    names = [s.session_name for s in sessions]
    assert "terminal-1" in names
    assert "broken-line-no-tabs" not in names


def test_parse_tmux_sessions_output_keeps_tabbed_path_intact() -> None:
    output = "terminal-1\t$3\t/weird\tpath\n"
    sessions = parse_tmux_sessions_output(output)
    assert sessions[0].cwd == "/weird\tpath"


def test_filter_user_terminal_sessions_drops_agent_prefixed_sessions() -> None:
    output = "terminal-1\t$3\t/a\nmngr-alice\t$1\t/b\nterminal-2\t$4\t/c\n"
    sessions = parse_tmux_sessions_output(output)
    user_terminals = filter_user_terminal_sessions(sessions, "mngr-")
    assert [s.session_name for s in user_terminals] == ["terminal-1", "terminal-2"]


def test_filter_user_terminal_sessions_keeps_everything_when_prefix_is_empty() -> None:
    output = "terminal-1\t$3\t/a\nmngr-alice\t$1\t/b\n"
    sessions = parse_tmux_sessions_output(output)
    user_terminals = filter_user_terminal_sessions(sessions, "")
    assert len(user_terminals) == 2


def test_allocate_next_terminal_name_starts_at_one() -> None:
    assert allocate_next_terminal_name(set(), "mngr-") == "terminal-1"


def test_allocate_next_terminal_name_fills_lowest_free_index() -> None:
    existing = {"terminal-1", "terminal-2", "mngr-alice"}
    assert allocate_next_terminal_name(existing, "mngr-") == "terminal-3"


def test_allocate_next_terminal_name_reuses_a_gap() -> None:
    existing = {"terminal-1", "terminal-3"}
    assert allocate_next_terminal_name(existing, "mngr-") == "terminal-2"


def test_is_destroyable_terminal_session_allows_terminals_and_blocks_agents() -> None:
    assert is_destroyable_terminal_session("terminal-1", "mngr-") is True
    assert is_destroyable_terminal_session("mngr-alice", "mngr-") is False
    assert is_destroyable_terminal_session("", "mngr-") is False


def test_is_destroyable_terminal_session_blocks_agents_even_with_terminal_like_names() -> None:
    # A custom prefix must still protect agent sessions.
    assert is_destroyable_terminal_session("ws-alice", "ws-") is False
    assert is_destroyable_terminal_session("terminal-1", "ws-") is True


def test_service_session_suffix_distinguishes_browser_panes() -> None:
    # A browser-fleet iframe carries ?session=<id> so each browser is a distinct
    # ref (service:browser?session=2); every other service iframe stays bare.
    assert _service_session_suffix(f"http://browser.{_LOCAL_WORKSPACE_HOST}/?session=2") == "?session=2"
    assert _service_session_suffix(f"http://browser.{_LOCAL_WORKSPACE_HOST}/?session=0") == "?session=0"
    assert _service_session_suffix(f"http://browser.{_LOCAL_WORKSPACE_HOST}/") == ""
    assert _service_session_suffix(f"http://web.{_LOCAL_WORKSPACE_HOST}/") == ""
    assert _service_session_suffix(None) == ""


def test_service_name_from_url_requires_the_workspace_coordinate() -> None:
    """A URL names a service only when a ``host-<32hex>`` label sits past the
    first label; the bare workspace origin and external hosts yield None."""
    assert _service_name_from_url(f"http://web.{_LOCAL_WORKSPACE_HOST}/") == "web"
    assert _service_name_from_url(f"https://api.{_SHARED_WORKSPACE_HOST}/health") == "api"
    assert _service_name_from_url(f"https://api.{_WORKSPACE_KEYED_SHARED_HOST}/health") == "api"
    # The bare workspace origin is the shell itself, not a service.
    assert _service_name_from_url(f"http://{_LOCAL_WORKSPACE_HOST}/") is None
    assert _service_name_from_url(f"https://{_WORKSPACE_KEYED_SHARED_HOST}/") is None
    # External panels never masquerade as services.
    assert _service_name_from_url("https://example.com/") is None
    assert _service_name_from_url("https://host-abc.example.com/") is None
    assert _service_name_from_url(None) is None


def test_service_name_from_url_agrees_with_layout_script_parser() -> None:
    """Drift guard: ``system/scripts/layout.py`` re-implements this parse.

    The script must stay stdlib-only (agents run it bare), so it cannot import
    this package; this test pins the two copies to each other instead. If it
    fails, the workspace-hostname convention changed in one place but not the
    other.
    """
    script_path = Path(__file__).parents[4] / "scripts" / "layout.py"
    spec = importlib.util.spec_from_file_location("_layout_script_drift_check", script_path)
    assert spec is not None and spec.loader is not None
    layout_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(layout_script)
    urls = (
        f"http://api.{_LOCAL_WORKSPACE_HOST}/health",
        f"http://web.{_LOCAL_WORKSPACE_HOST}/",
        f"https://api.{_SHARED_WORKSPACE_HOST}/health",
        f"https://api.{_WORKSPACE_KEYED_SHARED_HOST}/health",
        "https://example.com/",
        f"http://{_LOCAL_WORKSPACE_HOST}/",
        f"https://{_WORKSPACE_KEYED_SHARED_HOST}/",
        "https://host-abc.example.com/",
        f"http://terminal.{_LOCAL_WORKSPACE_HOST}/?arg=_&arg=agent&arg=main",
    )
    for url in urls:
        coordinates = layout_script._service_coordinates_from_url(url)
        assert _service_name_from_url(url) == (coordinates[0] if coordinates else None), url


def test_labeled_service_origin_round_trips_to_service_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``<name>-<rand>`` origin label maps back to its registered service name.

    Panel origins are built from the unguessable ``<name>-<rand>`` label, so the
    stored URL's first hostname label is that label; the registry inverts it to
    the service name. An unregistered label falls back to itself (best effort),
    and the ``layout.py`` mirror agrees on both.
    """
    apps_file = tmp_path / "apps.toml"
    apps_file.write_text('[[apps]]\nname = "terminal"\nurl = "http://localhost:7681"\nlabel = "terminal-x7k9q2w1"\n')
    monkeypatch.setenv("MINDS_APPS_FILE", str(apps_file))

    labeled_url = f"http://terminal-x7k9q2w1.{_LOCAL_WORKSPACE_HOST}/?arg=_&arg=agent&arg=main"
    assert _service_name_from_url(labeled_url) == "terminal"

    # An unknown label (no registry row) falls back to itself.
    unknown_url = f"http://ghost-00000000.{_LOCAL_WORKSPACE_HOST}/"
    assert _service_name_from_url(unknown_url) == "ghost-00000000"

    # The stdlib ``layout.py`` mirror must agree on the same registry.
    script_path = Path(__file__).parents[4] / "scripts" / "layout.py"
    spec = importlib.util.spec_from_file_location("_layout_script_label_check", script_path)
    assert spec is not None and spec.loader is not None
    layout_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(layout_script)
    for url in (labeled_url, unknown_url):
        coordinates = layout_script._service_coordinates_from_url(url)
        assert coordinates is not None
        assert coordinates[0] == _service_name_from_url(url), url


def test_terminal_origin_label_reads_the_registry_and_degrades_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The label is the only way to address the terminal, and it is read, not assumed.

    Callers use it to offer a terminal from a surface that has no application
    to open one with, so every way of not finding it has to answer "no terminal
    to offer" rather than raise: the registry can be absent (a workspace that
    has not registered its services yet), unparseable, or carry no terminal row
    at all, and a row whose label could not be a hostname could not name an
    origin either.
    """
    apps_file = tmp_path / "apps.toml"
    monkeypatch.setenv("MINDS_APPS_FILE", str(apps_file))

    assert terminal_origin_label() is None, "a registry that does not exist yet"

    apps_file.write_text('[[apps]]\nname = "browser"\nurl = "http://localhost:8081"\nlabel = "browser-aaaa1111"\n')
    assert terminal_origin_label() is None, "a registry with no terminal row"

    apps_file.write_text("this is not toml [[[")
    assert terminal_origin_label() is None, "a registry that cannot be parsed"

    apps_file.write_text('[[apps]]\nname = "terminal"\nurl = "http://localhost:7681"\nlabel = "terminal-x7k9q2w1"\n')
    assert terminal_origin_label() == "terminal-x7k9q2w1"

    # Not a hostname label, so it cannot be prefixed onto the workspace
    # coordinate -- and it is untrusted input to whatever renders it.
    apps_file.write_text('[[apps]]\nname = "terminal"\nurl = "http://localhost:7681"\nlabel = "not/a<label>"\n')
    assert terminal_origin_label() is None
