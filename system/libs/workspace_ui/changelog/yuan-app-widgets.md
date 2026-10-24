The app contract gains a widget surface: a handshake says whether it frames a window or a widget, and a widget page takes `shell:widget-mode` and sends `shell:widget-size` and `shell:widget-hidden` (`onWidgetMode`, `widgetSize`, `widgetHidden`, `surface()`). A widget sends no location or focus report.

A widget's page gives an element a tooltip with `data-widget-tooltip`: `connectToShell` reports it to the shell as `shell:widget-tooltip`, which shows the workspace's tooltip outside the widget's frame. The shared tooltip gains `showTooltipAtRect` and `hideTooltipAtRect` for such a trigger, whose pointer the document cannot hear.

A press anywhere on a widget's page drags the widget: once it travels a few pixels, `connectToShell` reports it to the shell as `shell:widget-drag` and swallows the click that ends it, with no code in the page. A short press stays the page's click, and a press on a button, a field, or an element marked `data-no-drag` never drags.

The app contract gains mode `tray` for a widget page whose manifest declares a tray form, and three page-to-shell messages `connectToShell` sends by itself: `shell:widget-menu` for a right-click, `shell:widget-press` in place of a click in the tray, and `shell:widget-dismiss` for Escape. A tray widget does not drag.

`connection.placeWidget(placement)` lets any page of an app that declares a widget ask the shell to put the app's widget on the desktop or in the taskbar's tray for this client (`shell:widget-placement`).

The contract gains mode and placement `toolbar`, the taskbar's row beside the launcher, for a widget page whose manifest declares a toolbar form; in it, as in the tray, a click is a press and the widget does not drag.

A tooltip shown by rectangle moves to another element of the same widget that offers the same text, rather than staying under the first.
