"""Tests for the PTY sign-in machinery that is not specific to one harness."""

from imbue.system_interface.harnesses.pty_auth import terminal_env


def test_the_spawned_terminal_is_described_as_its_own() -> None:
    """Inheriting the parent's terminal NAME is what broke agy's sign-in.

    With `TERM_PROGRAM=tmux` inherited from the tmux session the workspace's server runs
    under, agy stopped emitting the OSC 8 hyperlink its OAuth URL is recovered from -- and
    the flow failed every time with nothing the CLI itself reported as an error. Node CLIs
    answer "may I use this feature" from the emulator's name via libraries like
    `supports-hyperlinks`, so a name describing somebody else's terminal is a wrong answer.
    """
    parent = {
        "PATH": "/usr/bin",
        "TERM": "tmux-256color",
        "TERM_PROGRAM": "tmux",
        "TERM_PROGRAM_VERSION": "3.5a",
        "TMUX": "/tmp/tmux-0/default,7,0",
        "TMUX_PANE": "%3",
        "HOME": "/home/user",
    }

    described = terminal_env(parent)

    assert described is not None
    assert "TERM_PROGRAM" not in described
    assert "TERM_PROGRAM_VERSION" not in described
    assert "TMUX" not in described
    assert "TMUX_PANE" not in described
    # TERM is pinned to a full-capability terminal, which is what the PTY actually is.
    assert described["TERM"] == "xterm-256color"
    # Everything else survives -- PATH above all, or the child never starts at all.
    assert described["PATH"] == "/usr/bin"
    assert described["HOME"] == "/home/user"


def test_no_environment_stays_no_environment() -> None:
    """None means "inherit the parent wholesale", which is pexpect's own default."""
    assert terminal_env(None) is None
