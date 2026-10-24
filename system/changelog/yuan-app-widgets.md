App widgets: added the plan (`docs/system/blueprint/app-widgets/plan-app-widgets.md`) and its additions to the desktop interface contracts, the stop-when-no-windows spec, and the pinned-entry and phone plans. `forward_port.py` copies a manifest's `[widget]` table onto the registry row.

`forward_port.py` copies `[widget]`'s `tray` onto the registry row.

`forward_port.py` copies `[widget]`'s `toolbar` onto the registry row.

The desktop interface contracts name `tray` and `toolbar` among the registry row's `widget` keys, and say a client with no presentation of a widget draws it on the desktop.
