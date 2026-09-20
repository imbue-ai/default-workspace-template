import pytest
from app_instances.primitives import MAX_INSTANCE_KEY_LENGTH

from terminal_app.errors import InvalidTerminalValueError
from terminal_app.primitives import (
    ClientTty,
    TerminalTabId,
    TmuxSessionName,
    Workdir,
    derive_terminal_title,
    instance_url_for_session,
    pty_path_for_session,
)


@pytest.mark.parametrize(
    "value",
    ["terminal-1", "build", "My-Build", "a_b", "x" * MAX_INSTANCE_KEY_LENGTH, "9"],
)
def test_tmux_session_name_accepts_names_tmux_and_the_key_rule_both_allow(
    value: str,
) -> None:
    assert TmuxSessionName(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "my session",
        "dot.ted",
        "colon:ed",
        "-leading",
        "x" * (MAX_INSTANCE_KEY_LENGTH + 1),
        "snéak",
    ],
)
def test_tmux_session_name_rejects_what_tmux_or_the_key_rule_refuses(
    value: str,
) -> None:
    with pytest.raises(InvalidTerminalValueError, match="invalid session name"):
        TmuxSessionName(value)


@pytest.mark.parametrize(
    "value", ["term-3f9c2b1e-6d4a-4a5e-9f21-0a1b2c3d4e5f", "tab-0123456789abcdef"]
)
def test_terminal_tab_id_accepts_the_shells_ids_and_other_key_shaped_ids(
    value: str,
) -> None:
    assert TerminalTabId(value) == value


@pytest.mark.parametrize("value", ["", "../escape", "has space", ".hidden"])
def test_terminal_tab_id_rejects_ids_that_cannot_name_a_file_safely(value: str) -> None:
    with pytest.raises(InvalidTerminalValueError, match="invalid tab id"):
        TerminalTabId(value)


def test_client_tty_is_a_device_path() -> None:
    assert ClientTty("/dev/pts/7") == "/dev/pts/7"
    for bad in ("", "pts/7", "/dev/pts/7 ", "/dev/\x01"):
        with pytest.raises(InvalidTerminalValueError, match="invalid client tty"):
            ClientTty(bad)


def test_workdir_rejects_empty_and_control_characters() -> None:
    assert Workdir("/home/user/workspace") == "/home/user/workspace"
    for bad in ("", "/tmp/\n", "x" * 1025):
        with pytest.raises(InvalidTerminalValueError, match="invalid workdir"):
            Workdir(bad)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("terminal-3", "Terminal 3"),
        ("terminal-03", "Terminal 03"),
        ("terminal-x", "terminal-x"),
        ("build", "build"),
        ("My-Build", "My-Build"),
    ],
)
def test_derive_terminal_title_matches_the_frontends_derived_name_rule(
    name: str, expected: str
) -> None:
    assert derive_terminal_title(TmuxSessionName(name)) == expected


def test_instance_url_is_the_wrapper_page_with_the_tab_placeholder() -> None:
    assert instance_url_for_session(TmuxSessionName("terminal-2")) == "/?session=terminal-2&tab={tab}"


def test_pty_path_carries_the_ttyd_arguments_in_dispatch_order() -> None:
    assert (
        pty_path_for_session(TmuxSessionName("terminal-2"), TerminalTabId("tab-0123"), None)
        == "/?arg=_&arg=session&arg=terminal-2&arg=tab-0123"
    )


def test_pty_path_keeps_an_empty_tab_slot_before_the_workdir() -> None:
    assert (
        pty_path_for_session(TmuxSessionName("build"), None, Workdir("/home/user/my project"))
        == "/?arg=_&arg=session&arg=build&arg=&arg=%2Fhome%2Fuser%2Fmy%20project"
    )
