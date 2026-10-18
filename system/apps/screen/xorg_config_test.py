from inline_snapshot import snapshot

from screen.xorg_config import cvt_modeline
from screen.xorg_config import render_xorg_conf


def test_cvt_modelines_match_the_cvt_tool() -> None:
    # Reference values from ``cvt <w> <h> 60`` (xserver-xorg-core).
    assert cvt_modeline(1920, 1080).render() == snapshot(
        'Modeline "1920x1080_60" 173.00 1920 2048 2248 2576 1080 1083 1088 1120 -HSync +VSync'
    )
    assert cvt_modeline(1600, 900).render() == snapshot(
        'Modeline "1600x900_60" 118.25 1600 1688 1856 2112 900 903 908 934 -HSync +VSync'
    )
    assert cvt_modeline(1280, 720).render() == snapshot(
        'Modeline "1280x720_60" 74.50 1280 1344 1472 1664 720 723 728 748 -HSync +VSync'
    )
    assert cvt_modeline(1024, 768).render() == snapshot(
        'Modeline "1024x768_60" 63.50 1024 1072 1176 1328 768 771 775 798 -HSync +VSync'
    )


def test_xorg_conf_renders_the_dummy_screen_with_the_seeded_modes() -> None:
    assert render_xorg_conf() == snapshot("""\
Section "ServerFlags"
  Option "DontVTSwitch" "true"
  Option "AllowMouseOpenFail" "true"
  Option "PciForceNone" "true"
  Option "AutoEnableDevices" "false"
  Option "AutoAddDevices" "false"
EndSection

Section "Device"
  Identifier "screen_device"
  Driver "dummy"
  Option "ConstantDPI" "true"
  VideoRam 65536
EndSection

Section "Monitor"
  Identifier "screen_monitor"
  HorizSync 5.0 - 1000.0
  VertRefresh 5.0 - 200.0
  Modeline "1920x1080_60" 173.00 1920 2048 2248 2576 1080 1083 1088 1120 -HSync +VSync
  Modeline "1600x900_60" 118.25 1600 1688 1856 2112 900 903 908 934 -HSync +VSync
  Modeline "1280x720_60" 74.50 1280 1344 1472 1664 720 723 728 748 -HSync +VSync
EndSection

Section "Screen"
  Identifier "screen"
  Device "screen_device"
  Monitor "screen_monitor"
  DefaultDepth 24
  SubSection "Display"
    Viewport 0 0
    Depth 24
    Modes "1920x1080_60" "1600x900_60" "1280x720_60"
    Virtual 1920 1080
  EndSubSection
EndSection

Section "ServerLayout"
  Identifier "screen_layout"
  Screen "screen"
EndSection
""")
