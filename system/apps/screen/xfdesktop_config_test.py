from pathlib import Path

import pytest
from inline_snapshot import snapshot

from screen.errors import XfconfChannelError
from screen.xfdesktop_config import XFDESKTOP_CHANNEL_RELATIVE_PATH
from screen.xfdesktop_config import render_xfdesktop_channel
from screen.xfdesktop_config import write_xfdesktop_channel


def test_a_fresh_channel_turns_the_icons_off() -> None:
    assert render_xfdesktop_channel(None) == snapshot("""\
<?xml version="1.1" encoding="UTF-8"?>

<channel name="xfce4-desktop" version="1.0">
  <property name="desktop-icons" type="empty">
    <property name="style" type="int" value="0" />
  </property>
</channel>
""")


def test_an_existing_channel_keeps_its_other_properties_and_overrides_the_icon_style() -> None:
    existing = """<?xml version="1.1" encoding="UTF-8"?>

<channel name="xfce4-desktop" version="1.0">
  <property name="backdrop" type="empty">
    <property name="screen0" type="empty">
      <property name="monitorDUMMY0" type="empty">
        <property name="workspace0" type="empty">
          <property name="last-image" type="string" value="/usr/share/backgrounds/xfce/xfce-shapes.svg"/>
        </property>
      </property>
    </property>
  </property>
  <property name="desktop-icons" type="empty">
    <property name="style" type="int" value="2"/>
    <property name="icon-size" type="uint" value="48"/>
  </property>
</channel>
"""
    rendered = render_xfdesktop_channel(existing)
    assert rendered == snapshot("""\
<?xml version="1.1" encoding="UTF-8"?>

<channel name="xfce4-desktop" version="1.0">
  <property name="backdrop" type="empty">
    <property name="screen0" type="empty">
      <property name="monitorDUMMY0" type="empty">
        <property name="workspace0" type="empty">
          <property name="last-image" type="string" value="/usr/share/backgrounds/xfce/xfce-shapes.svg" />
        </property>
      </property>
    </property>
  </property>
  <property name="desktop-icons" type="empty">
    <property name="style" type="int" value="0" />
    <property name="icon-size" type="uint" value="48" />
  </property>
</channel>
""")
    assert render_xfdesktop_channel(rendered) == rendered


@pytest.mark.parametrize(
    "existing",
    [
        "not xml at all <",
        '<?xml version="1.0"?><channel name="xfce4-panel" version="1.0"/>',
        "<settings/>",
    ],
)
def test_a_file_that_is_not_the_channels_is_refused(existing: str) -> None:
    with pytest.raises(XfconfChannelError):
        render_xfdesktop_channel(existing)


def test_the_channel_is_written_into_the_home_and_rewritten_only_when_changed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    write_xfdesktop_channel(home)
    path = home / XFDESKTOP_CHANNEL_RELATIVE_PATH
    assert path.read_text() == render_xfdesktop_channel(None)
    written_at = path.stat().st_mtime_ns
    write_xfdesktop_channel(home)
    assert path.stat().st_mtime_ns == written_at


def test_a_channel_file_the_service_cannot_merge_is_left_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / XFDESKTOP_CHANNEL_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("<settings/>")
    write_xfdesktop_channel(home)
    assert path.read_text() == "<settings/>"
