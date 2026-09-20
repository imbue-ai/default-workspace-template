import pytest

from terminal_app.errors import InvalidTerminalValueError
from terminal_app.primitives import (
    MAX_SESSION_NAME_LENGTH,
    MAX_TERMINAL_TITLE_LENGTH,
    TerminalTitle,
    TmuxSessionName,
    Workdir,
    allocate_terminal_name,
    derive_terminal_title,
    pty_path_for_session,
    session_page_path,
)


@pytest.mark.parametrize(
    "value",
    ["terminal-1", "build", "My-Build", "a_b", "x" * MAX_SESSION_NAME_LENGTH, "9"],
)
def test_tmux_session_name_accepts_names_tmux_and_the_url_both_allow(value: str) -> None:
    assert TmuxSessionName(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "my session",
        "dot.ted",
        "colon:ed",
        "-leading",
        "x" * (MAX_SESSION_NAME_LENGTH + 1),
        "sn\u00e9ak",
    ],
)
def test_tmux_session_name_rejects_what_tmux_or_the_url_refuses(value: str) -> None:
    with pytest.raises(InvalidTerminalValueError, match="invalid session name"):
        TmuxSessionName(value)


def test_terminal_title_is_trimmed_non_blank_and_bounded() -> None:
    assert TerminalTitle("  Build  ") == "Build"
    with pytest.raises(InvalidTerminalValueError, match="must not be blank"):
        TerminalTitle("   ")
    with pytest.raises(InvalidTerminalValueError, match="over the 256-character limit"):
        TerminalTitle("x" * (MAX_TERMINAL_TITLE_LENGTH + 1))


def test_workdir_rejects_empty_and_control_characters() -> None:
    assert Workdir("/home/user/workspace") == "/home/user/workspace"
    for bad in ("", "/tmp/\n", "x" * 1025):
        with pytest.raises(InvalidTerminalValueError, match="invalid workdir"):
            Workdir(bad)


@pytest.mark.parametrize(
    ("taken", "expected"),
    [
        (set(), "terminal-1"),
        ({"terminal-1", "terminal-2"}, "terminal-3"),
        ({"terminal-1", "terminal-3"}, "terminal-2"),
        ({"terminal-0", "terminal-01", "build", "terminal-x"}, "terminal-1"),
    ],
)
def test_allocate_terminal_name_fills_the_lowest_gap(taken: set[str], expected: str) -> None:
    assert allocate_terminal_name(taken) == expected


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
def test_derive_terminal_title_matches_the_frontends_derived_name_rule(name: str, expected: str) -> None:
    assert derive_terminal_title(TmuxSessionName(name)) == expected


def test_session_page_path_is_the_wrapper_page_for_the_session() -> None:
    assert session_page_path(TmuxSessionName("terminal-2")) == "/?session=terminal-2"


def test_pty_path_carries_the_ttyd_arguments_in_dispatch_order() -> None:
    assert pty_path_for_session(TmuxSessionName("terminal-2"), None) == "/?arg=_&arg=session&arg=terminal-2"


def test_pty_path_escapes_the_workdir() -> None:
    assert (
        pty_path_for_session(TmuxSessionName("build"), Workdir("/home/user/my project"))
        == "/?arg=_&arg=session&arg=build&arg=%2Fhome%2Fuser%2Fmy%20project"
    )
