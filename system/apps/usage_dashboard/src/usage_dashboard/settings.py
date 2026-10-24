"""The Usage app's own settings: where the workspace's Usage widget goes (the desktop, the taskbar's row or its tray)
and how it looks there -- its style, size and how much it shows on the desktop, and its design in the taskbar.

One small JSON file in the app's data directory (``data/.apps/usage-dashboard/settings.json`` on a live workspace),
read on every request so an edit from another process lands without a restart, and written whole. A missing or
unreadable file reads as the defaults.
"""

import json
import os
import threading
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

SETTINGS_FILENAME: Final[str] = "settings.json"


class WidgetVariant(LowerCaseStrEnum):
    """A version of the Usage widget (app-widgets plan section 7.8)."""

    # Concentric rings per subscription: the weekly limit outside, the 5-hour limit inside.
    RINGS = auto()
    # The ring pairs alone, the 5-hour figure in each, with nothing else in view.
    MINIMAL = auto()
    # A row of horizontal bars per subscription: the 5-hour limit above the weekly one.
    BARS = auto()


class WidgetSize(LowerCaseStrEnum):
    """How large the Usage widget is drawn."""

    SMALL = auto()
    MEDIUM = auto()
    LARGE = auto()


class WidgetDetail(LowerCaseStrEnum):
    """How much the Usage widget shows."""

    # The 5-hour limit alone, and no pay-as-you-go spend.
    LESS = auto()
    # Both limits and the spend; the rest is in tooltips.
    STANDARD = auto()
    # Also each limit's figure and reset time, always in view.
    MORE = auto()


class TrayDesign(LowerCaseStrEnum):
    """How the Usage widget is drawn in the taskbar's tray."""

    # A small ring pair per subscription beside its name over its 5-hour figure.
    RINGS = auto()
    # The ring pair alone, like a menu-bar icon.
    RING = auto()
    # The name over a 5-hour and a weekly bar, beside the 5-hour figure.
    BARS = auto()
    # The name and the 5-hour figure, as text alone.
    NUMBERS = auto()
    # The name beside the 5-hour figure over the weekly one, each with its colour's dot.
    STACKED = auto()


class WidgetPlace(LowerCaseStrEnum):
    """Where the Usage widget goes: the page picks this first, then shows the choices for that place."""

    # On the desktop, in its style and size.
    DESKTOP = auto()
    # In the taskbar's row beside the launcher, drawn like a window's entry.
    TOOLBAR = auto()
    # In the tray at the bar's right end.
    TRAY = auto()


class ToolbarDesign(LowerCaseStrEnum):
    """How the Usage widget is drawn in the taskbar's row, among the windows' entries."""

    # A chip like a window's entry: the ring pair as its icon, the name and the 5-hour figure as its title.
    CHIP = auto()
    # Upright 5-hour and weekly meters, as tall as an entry, beside the name and the 5-hour figure.
    METERS = auto()
    # The names and 5-hour figures as one line of text, like a window's title.
    INLINE = auto()
    # The name and the 5-hour figure over a thin line per window, each filled to its share.
    LINE = auto()


class UsageSettings(FrozenModel):
    """What the settings file holds. Every field has a default, so an older file reads whole."""

    widget_variant: WidgetVariant = Field(default=WidgetVariant.RINGS, description="The widget's style")
    widget_size: WidgetSize = Field(default=WidgetSize.MEDIUM, description="How large the widget is drawn")
    widget_detail: WidgetDetail = Field(default=WidgetDetail.STANDARD, description="How much the widget shows")
    widget_tray: TrayDesign = Field(default=TrayDesign.RINGS, description="The widget's design in the taskbar's tray")
    widget_place: WidgetPlace = Field(default=WidgetPlace.DESKTOP, description="Where the widget goes")
    widget_toolbar: ToolbarDesign = Field(
        default=ToolbarDesign.CHIP, description="The widget's design in the taskbar's row"
    )


class UsageSettingsStore(MutableModel):
    """The settings file: read whole on every read, written whole under a lock."""

    path: Path = Field(frozen=True, description="The settings file")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def read(self) -> UsageSettings:
        """The settings as they stand; an absent or unreadable file reads as the defaults."""
        if not self.path.exists():
            return UsageSettings()
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            logger.warning("Ignoring an unreadable Usage settings file at {}: {}", self.path, e)
            return UsageSettings()
        try:
            return UsageSettings.model_validate(payload)
        except ValidationError as e:
            logger.warning("Ignoring a Usage settings file at {} that does not fit the settings: {}", self.path, e)
            return UsageSettings()

    def write(self, settings: UsageSettings) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(".json.tmp")
            temp_path.write_text(json.dumps(settings.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
            os.replace(temp_path, self.path)
