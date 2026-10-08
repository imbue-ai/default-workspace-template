from collections.abc import Callable
from typing import Any

import pytest
from app_manifest.primitives import AppName

from workspace_layout.agent_identity import ENV_MINDS_CHAT_ID
from workspace_layout.agent_identity import ENV_MNGR_AGENT_ID
from workspace_layout.answers import ClientView
from workspace_layout.answers import DesktopOpAnswer
from workspace_layout.client import DisconnectedShell
from workspace_layout.client import ShellLayoutClient
from workspace_layout.client import requester_from_environment
from workspace_layout.errors import ShellAnswerMalformedError
from workspace_layout.errors import ShellRefusedOpError
from workspace_layout.errors import ShellUnreachableError
from workspace_layout.ops import ClientActivityReport
from workspace_layout.ops import NavigateArgs
from workspace_layout.ops import OpenArgs
from workspace_layout.ops import OpRequester
from workspace_layout.ops import PlaceArgs
from workspace_layout.ops import RefreshWindowArgs
from workspace_layout.ops import ShowArgs
from workspace_layout.ops import WindowArgs
from workspace_layout.primitives import ClientActivityKind
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import IfPresent
from workspace_layout.primitives import ShowOutcome
from workspace_layout.primitives import WindowId
from workspace_layout.primitives import WindowPage
from workspace_layout.primitives import WindowPath
from workspace_layout.records import Frame
from workspace_layout.shell_url import CLIENT_ACTIVITY_ROUTE
from workspace_layout.shell_url import CLIENTS_ROUTE
from workspace_layout.shell_url import DESKTOPS_ROUTE
from workspace_layout.shell_url import LAYOUT_OP_ROUTE
from workspace_layout.testing import FAKE_TIME
from workspace_layout.testing import LoopbackShell
from workspace_layout.testing import desktop_answer
from workspace_layout.testing import fake_desktop
from workspace_layout.testing import fake_window

_WINDOW = "win-0123456789abcdef"
_CLIENT = ClientId("client-1")
_REQUESTER = OpRequester(app=AppName("chat"), marker="")
_SHOW = ShowArgs(
    app=AppName("chat"),
    path=WindowPath("/?chat=agent-1"),
    showing=(WindowPath("/agent-1"),),
    repoint=(WindowPage("/"),),
    client=_CLIENT,
)
_SHOWN_ANSWER = {**desktop_answer(fake_desktop("home"), "client-1", _WINDOW), "shown": "navigated"}
_ACTIVITY = ClientActivityReport(
    client_id=_CLIENT,
    desktop_id=DesktopId("home"),
    kind=ClientActivityKind.MESSAGE,
    app="chat",
    key="agent-1",
    text="hi",
)


def _client(shell_url: str) -> ShellLayoutClient:
    return ShellLayoutClient(shell_url=shell_url, requester=_REQUESTER, timeout_seconds=2.0)


def _unreachable_url(loopback_shell: LoopbackShell) -> str:
    url = loopback_shell.url
    loopback_shell.close()
    return url


def test_a_show_posts_the_show_op_under_the_requester_and_answers_the_shells_shown_and_window(
    loopback_shell: LoopbackShell,
) -> None:
    loopback_shell.op_answer = _SHOWN_ANSWER

    answer = _client(loopback_shell.url).show(_SHOW)

    assert (answer.desktop_id, answer.client_id, answer.window_id, answer.shown) == (
        DesktopId("home"),
        _CLIENT,
        WindowId(_WINDOW),
        ShowOutcome.NAVIGATED,
    )
    assert loopback_shell.posted == [
        (
            LAYOUT_OP_ROUTE,
            {
                "op": "show",
                "args": {
                    "app": "chat",
                    "path": "/?chat=agent-1",
                    "showing": ["/agent-1"],
                    "repoint": ["/"],
                    "client": "client-1",
                },
                "requester": {"app": "chat", "marker": ""},
            },
        )
    ]
    assert loopback_shell.posted_content_types == ["application/json"]


def test_an_answer_carrying_fields_the_library_does_not_know_still_reads(loopback_shell: LoopbackShell) -> None:
    """A newer shell may add to its answers, at the top and inside the records they carry."""
    window = {**fake_window(_WINDOW, "chat", "/?chat=agent-1").model_dump(mode="json"), "is_popped_out": True}
    desktop = {**fake_desktop("home").model_dump(mode="json"), "windows": [window]}
    loopback_shell.op_answer = {**_SHOWN_ANSWER, "desktop": desktop, "is_raised_in_own_window": True}

    answer = _client(loopback_shell.url).show(_SHOW)

    assert [str(window.path) for window in answer.desktop.windows] == ["/?chat=agent-1"]


@pytest.mark.parametrize(
    ("call", "op", "expected_args"),
    [
        (
            lambda client: client.open(
                OpenArgs(
                    app=AppName("browser"),
                    path=WindowPath("/?session=browser-1"),
                    if_present=IfPresent.FOCUS,
                    minimized=True,
                )
            ),
            "open",
            {"app": "browser", "path": "/?session=browser-1", "if_present": "focus", "minimized": True},
        ),
        (
            lambda client: client.focus(WindowArgs(window="self", client=_CLIENT)),
            "focus",
            {"window": "self", "client": "client-1"},
        ),
        (
            lambda client: client.navigate(NavigateArgs(window="files", path=WindowPath("/b/"))),
            "navigate",
            {"window": "files", "path": "/b/"},
        ),
        (
            lambda client: client.place(
                PlaceArgs(
                    window=_WINDOW,
                    frame=Frame(x=0.07, y=0.05, width=0.38, height=0.9),
                    client=_CLIENT,
                    desktop="home",
                )
            ),
            "place",
            {
                "window": _WINDOW,
                "frame": {"x": 0.07, "y": 0.05, "width": 0.38, "height": 0.9},
                "client": "client-1",
                "desktop": "home",
            },
        ),
        (
            lambda client: client.close(WindowArgs(window=_WINDOW)),
            "close",
            {"window": _WINDOW},
        ),
    ],
    ids=["open", "focus", "navigate", "place", "close"],
)
def test_each_document_op_posts_its_body_and_answers_the_window_the_shell_named(
    loopback_shell: LoopbackShell,
    call: Callable[[ShellLayoutClient], DesktopOpAnswer],
    op: str,
    expected_args: dict[str, Any],
) -> None:
    loopback_shell.op_answer = desktop_answer(fake_desktop("home"), "client-1", _WINDOW)

    answer = call(_client(loopback_shell.url))

    assert answer.window_id == WindowId(_WINDOW) and answer.desktop_id == DesktopId("home")
    assert loopback_shell.posted_ops() == [(op, expected_args)]


def test_an_open_answered_with_no_window_is_malformed(loopback_shell: LoopbackShell) -> None:
    loopback_shell.op_answer = desktop_answer(fake_desktop("home"), "client-1", None)
    request = OpenArgs(app=AppName("files"), path=WindowPath("/"))

    with pytest.raises(ShellAnswerMalformedError, match="open"):
        _client(loopback_shell.url).open(request)


def test_a_refresh_posts_the_window_and_answers_the_client_it_reached(loopback_shell: LoopbackShell) -> None:
    answer = _client(loopback_shell.url).refresh(RefreshWindowArgs(window="self"))

    assert answer.target_client_id == ClientId("c1")
    assert loopback_shell.posted_ops() == [("refresh", {"window": "self"})]


def test_a_shell_that_refuses_an_op_raises_quoting_the_status_and_the_refusal(loopback_shell: LoopbackShell) -> None:
    loopback_shell.op_refusal = (404, {"detail": "No client 'client-1'"})

    with pytest.raises(ShellRefusedOpError, match=r"refused the show \(404\): No client 'client-1'") as raised:
        _client(loopback_shell.url).show(_SHOW)

    assert raised.value.status_code == 404
    assert len(loopback_shell.posted) == 1


def test_a_redirect_is_answered_as_it_came_rather_than_followed(loopback_shell: LoopbackShell) -> None:
    loopback_shell.answer_headers = {"Location": "/moved"}
    loopback_shell.get_answers = {DESKTOPS_ROUTE: (302, "moved"), "/moved": (200, {"desktops": []})}
    loopback_shell.op_refusal = (302, "moved")
    client = _client(loopback_shell.url)

    with pytest.raises(ShellRefusedOpError) as refused_op:
        client.show(_SHOW)
    with pytest.raises(ShellRefusedOpError) as refused_read:
        client.desktops()

    assert (refused_op.value.status_code, refused_read.value.status_code) == (302, 302)


def test_a_shell_that_cannot_be_reached_raises_unreachable(loopback_shell: LoopbackShell) -> None:
    client = _client(_unreachable_url(loopback_shell))

    with pytest.raises(ShellUnreachableError, match="Could not reach the shell"):
        client.show(_SHOW)
    with pytest.raises(ShellUnreachableError):
        client.connected_clients()


def test_a_shell_url_without_a_scheme_raises_unreachable() -> None:
    with pytest.raises(ShellUnreachableError, match="unknown url type"):
        _client("").show(_SHOW)


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        [],
        {"ok": True},
        {**_SHOWN_ANSWER, "window_id": None},
        {**_SHOWN_ANSWER, "window_id": "win-1"},
        {**_SHOWN_ANSWER, "shown": "tiled"},
    ],
    ids=["not-json", "a-list", "no-shown", "no-window", "not-a-window-id", "unknown-shown"],
)
def test_a_2xx_to_a_show_that_is_not_a_show_answer_is_malformed(loopback_shell: LoopbackShell, answer: Any) -> None:
    loopback_shell.op_answer = answer

    with pytest.raises(ShellAnswerMalformedError):
        _client(loopback_shell.url).show(_SHOW)


def test_the_connected_clients_are_the_listed_clients_holding_a_socket(loopback_shell: LoopbackShell) -> None:
    on_home = ClientView(id=ClientId("c1"), active_desktop=DesktopId("home"), last_seen=FAKE_TIME, is_connected=True)
    away = ClientView(id=ClientId("c2"), last_seen=FAKE_TIME, is_connected=False)
    loopback_shell.get_answers[CLIENTS_ROUTE] = (
        200,
        {
            "clients": [
                on_home.model_dump(mode="json"),
                away.model_dump(mode="json"),
                # One entry the contract does not allow is skipped rather than hiding the others.
                {"id": "", "is_connected": True},
            ]
        },
    )

    assert _client(loopback_shell.url).connected_clients() == [on_home]


@pytest.mark.parametrize(
    "body",
    [{}, {"clients": {"c1": True}}, {"clients": "c1"}, [{"id": "c1", "is_connected": True}], {"clients": ["c1", {}]}],
    ids=["no-key", "a-map", "a-string", "a-bare-list", "no-entry-readable"],
)
def test_a_client_list_of_the_wrong_shape_is_malformed_rather_than_nobody(
    loopback_shell: LoopbackShell, body: Any
) -> None:
    loopback_shell.get_answers[CLIENTS_ROUTE] = (200, body)

    with pytest.raises(ShellAnswerMalformedError):
        _client(loopback_shell.url).connected_clients()


def test_an_empty_client_list_is_nobody(loopback_shell: LoopbackShell) -> None:
    loopback_shell.get_answers[CLIENTS_ROUTE] = (200, {"clients": []})

    assert _client(loopback_shell.url).connected_clients() == []


def test_the_desktops_are_listed_in_the_shells_order(loopback_shell: LoopbackShell) -> None:
    desktops = [fake_desktop("home"), fake_desktop("work")]
    loopback_shell.get_answers[DESKTOPS_ROUTE] = (
        200,
        {"desktops": [desktop.model_dump(mode="json") for desktop in desktops]},
    )

    assert _client(loopback_shell.url).desktops() == desktops


def test_client_activity_is_posted_to_the_shells_activity_route(loopback_shell: LoopbackShell) -> None:
    _client(loopback_shell.url).record_client_activity(_ACTIVITY)

    assert loopback_shell.posted == [
        (
            CLIENT_ACTIVITY_ROUTE,
            {
                "client_id": "client-1",
                "desktop_id": "home",
                "kind": "message",
                "app": "chat",
                "key": "agent-1",
                "text": "hi",
            },
        )
    ]


def test_a_client_activity_report_the_shell_refuses_is_a_warning_quoting_the_refusal(
    loopback_shell: LoopbackShell, loguru_records: list[str]
) -> None:
    loopback_shell.activity_status = 400

    _client(loopback_shell.url).record_client_activity(_ACTIVITY)

    assert any(record.startswith("WARNING") and "(400): refused" in record for record in loguru_records)


@pytest.mark.parametrize("is_failing_rather_than_down", [True, False], ids=["failing", "down"])
def test_a_client_activity_report_a_shell_cannot_take_is_a_debug_log_and_no_error(
    loopback_shell: LoopbackShell, loguru_records: list[str], is_failing_rather_than_down: bool
) -> None:
    loopback_shell.activity_status = 500
    url = loopback_shell.url if is_failing_rather_than_down else _unreachable_url(loopback_shell)

    _client(url).record_client_activity(_ACTIVITY)

    assert any(record.startswith("DEBUG") for record in loguru_records)
    assert not any(record.startswith("WARNING") for record in loguru_records)


def test_the_disconnected_shell_reaches_nobody() -> None:
    shell = DisconnectedShell()

    assert shell.connected_clients() == []
    assert shell.desktops() == []
    shell.record_client_activity(_ACTIVITY)
    with pytest.raises(ShellUnreachableError):
        shell.show(_SHOW)
    with pytest.raises(ShellUnreachableError):
        shell.focus(WindowArgs(window="self"))


def test_the_requester_is_the_calling_agents_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    assert requester_from_environment() is None
    monkeypatch.setenv(ENV_MNGR_AGENT_ID, "agent-42")
    assert requester_from_environment() == OpRequester(app=AppName("chat"), marker="agent-42")
    # An agent the chat app created carries its chat's id, which is not its own id once a chat has handed off
    # between agents.
    monkeypatch.setenv(ENV_MINDS_CHAT_ID, "agent-41")
    assert requester_from_environment() == OpRequester(app=AppName("chat"), marker="agent-41")
