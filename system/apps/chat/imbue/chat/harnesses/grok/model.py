"""grok's model catalog, which offers no models: the model bar does not switch a grok agent."""

from typing import Final

from imbue.chat.harnesses.model import HarnessCatalog
from imbue.chat.harnesses.model import PickerMode
from imbue.chat.harnesses.model import SwitchMode

GROK_CATALOG: Final[HarnessCatalog] = HarnessCatalog(
    options=(),
    switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
    picker_mode=PickerMode.LIST,
    powered_by_text="Powered by Grok Build",
    # The tap cancels the turn with one Ctrl+C and the flush worker types the held messages; nothing restarts.
    native_atomic_shoulder_tap_possible=True,
)
