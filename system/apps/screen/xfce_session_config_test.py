from pathlib import Path

import pytest
from inline_snapshot import snapshot

from screen.errors import XfconfChannelError
from screen.xfce_session_config import XFCE_SESSION_CHANNEL_RELATIVE_PATH
from screen.xfce_session_config import render_xfce_session_channel
from screen.xfce_session_config import write_xfce_session_channel


def test_a_fresh_channel_names_the_panel_less_session_as_the_failsafe_one_and_never_saves() -> None:
    assert render_xfce_session_channel(None) == snapshot("""\
<?xml version="1.1" encoding="UTF-8"?>

<channel name="xfce4-session" version="1.0">
  <property name="general" type="empty">
    <property name="FailsafeSessionName" type="string" value="ScreenNoPanel" />
    <property name="SaveOnExit" type="bool" value="false" />
  </property>
  <property name="sessions" type="empty">
    <property name="ScreenNoPanel" type="empty">
      <property name="IsFailsafe" type="bool" value="true" />
      <property name="Count" type="int" value="3" />
      <property name="Client0_Command" type="array">
        <value type="string" value="xfwm4" />
      </property>
      <property name="Client0_Priority" type="int" value="15" />
      <property name="Client0_PerScreen" type="bool" value="false" />
      <property name="Client1_Command" type="array">
        <value type="string" value="xfsettingsd" />
      </property>
      <property name="Client1_Priority" type="int" value="20" />
      <property name="Client1_PerScreen" type="bool" value="false" />
      <property name="Client2_Command" type="array">
        <value type="string" value="xfdesktop" />
      </property>
      <property name="Client2_Priority" type="int" value="35" />
      <property name="Client2_PerScreen" type="bool" value="false" />
    </property>
  </property>
</channel>
""")


def test_an_existing_channel_keeps_its_other_properties_and_replaces_the_session_whole() -> None:
    existing = """<?xml version="1.1" encoding="UTF-8"?>

<channel name="xfce4-session" version="1.0">
  <property name="general" type="empty">
    <property name="FailsafeSessionName" type="string" value="Failsafe"/>
    <property name="LockCommand" type="string" value="xflock4"/>
    <property name="SaveOnExit" type="bool" value="true"/>
  </property>
  <property name="sessions" type="empty">
    <property name="ScreenNoPanel" type="empty">
      <property name="Count" type="int" value="5"/>
      <property name="Client4_Command" type="array">
        <value type="string" value="xfce4-panel"/>
      </property>
    </property>
  </property>
</channel>
"""
    rendered = render_xfce_session_channel(existing)
    assert 'name="LockCommand" type="string" value="xflock4"' in rendered
    assert "xfce4-panel" not in rendered
    assert rendered == render_xfce_session_channel(rendered)
    assert rendered.replace('    <property name="LockCommand" type="string" value="xflock4" />\n', "") == (
        render_xfce_session_channel(None)
    )


def test_a_file_that_is_not_the_session_channel_is_refused() -> None:
    with pytest.raises(XfconfChannelError, match="not well-formed"):
        render_xfce_session_channel("<channel")
    with pytest.raises(XfconfChannelError, match="xfce4-session channel"):
        render_xfce_session_channel('<channel name="xfce4-desktop" version="1.0"/>')


def test_writing_the_channel_into_a_home_creates_it_and_leaves_an_unmergeable_one_alone(tmp_path: Path) -> None:
    write_xfce_session_channel(tmp_path)
    path = tmp_path / XFCE_SESSION_CHANNEL_RELATIVE_PATH
    assert path.read_text() == render_xfce_session_channel(None)

    path.write_text("not xml")
    write_xfce_session_channel(tmp_path)
    assert path.read_text() == "not xml"
