from inline_snapshot import snapshot

from screen.windows import parse_wmctrl_list
from screen.windows import window_title_from_tool_output

_WMCTRL_OUTPUT = """\
0x01400003  0 1234   0    0    1920 28   xfce4-panel.Xfce4-panel  box xfce4-panel
0x02200007 -1 0      0    0    1920 1080 xfdesktop.Xfdesktop  box Desktop
0x03400007  0 5678   120  80   800  600  xfce4-terminal.Xfce4-terminal  box josh@box: ~/workspace - Terminal
0x03600001  0 9012   400  300  150  100  xeyes.XEyes  box xeyes
this line does not fit
"""


def test_wmctrl_list_is_parsed_with_titles_that_hold_spaces() -> None:
    windows = parse_wmctrl_list(_WMCTRL_OUTPUT)
    assert [(window.id, window.window_class, window.title) for window in windows] == snapshot(
        [
            ("0x01400003", "xfce4-panel.Xfce4-panel", "xfce4-panel"),
            ("0x02200007", "xfdesktop.Xfdesktop", "Desktop"),
            ("0x03400007", "xfce4-terminal.Xfce4-terminal", "josh@box: ~/workspace - Terminal"),
            ("0x03600001", "xeyes.XEyes", "xeyes"),
        ]
    )
    terminal = windows[2]
    assert (terminal.desktop, terminal.pid, terminal.x, terminal.y, terminal.width, terminal.height) == (
        0,
        5678,
        120,
        80,
        800,
        600,
    )
    assert windows[1].desktop == -1


def test_empty_output_lists_no_windows() -> None:
    assert parse_wmctrl_list("") == []


def test_active_window_title_is_none_for_an_empty_answer() -> None:
    assert window_title_from_tool_output("") is None
    assert window_title_from_tool_output("Firefox\n") == "Firefox"
