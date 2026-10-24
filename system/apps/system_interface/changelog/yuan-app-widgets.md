App widgets: an app can declare a `[widget]` page that the desktop keeps on screen, above every window.

- Each enabled widget is drawn top right by default as a light panel with no title bar, at the size its page reports. Dragging the widget, by its rim or anywhere on its page, moves it anywhere on the desktop, where it stays (a plain click on it still opens its app); double-clicking the rim collapses or expands it, and right-clicking the rim opens a menu that collapses it, opens the app, or hides the widget for everyone on the workspace. Widgets not yet moved stack down from the top right, starting below a title bar so a maximized window's controls stay clear. A moved widget near an edge grows away from it when it changes size.

- Each client's widget positions and collapsed state are kept on its client record (`POST /api/clients/<id>/widgets/<app>`). Whether a widget shows is one switch for the whole workspace (`widgets.json`, `GET`/`POST /api/widgets`), also offered as Show/Hide widget in an app's shortcut and launcher menus. Both are listed in the inventory and pushed as `client_widgets_changed` and `widgets_updated`.

- A widget's tooltips are shown with the desktop's own tooltip, outside the widget, so the small panel never clips them.

- Clicking a widget raises any open window of its app, whatever page that window is on, and opens one only when the app has none.

- On a phone, widgets are drawn collapsed in a strip above the bar.

- An enabled widget keeps its app running, as an open window does, even when the app declares `stop_when_no_windows`.

- A widget whose page draws a tray form can live in the taskbar's tray instead: "Move to taskbar" in its menu (and "Move to desktop" back), kept per client. In the tray a click opens a popover above the bar with the widget expanded; a click elsewhere, Escape, or opening the app closes it. A right-click anywhere on a widget, not only its edge, now opens its menu.

- A page of an app can move the app's widget to the tray or back for its client (`shell:widget-placement`), so an app can offer that choice in its own settings.

- A widget whose page draws a toolbar form can also sit in the taskbar's own row, right after the launcher and before the window entries, as tall as an entry and tinted like one; its popover opens above that end of the bar. The widget's menu offers each place it is not: "Move to desktop", "Move to toolbar", "Move to tray" (the last was "Move to taskbar").

- Writing a client's presentation of a widget for an app that declares none answers 404, as showing or hiding that widget does.

- A taskbar widget's open popover closes when the widget leaves the taskbar some other way (hidden for the workspace, moved by another window of the same client, or withdrawn), so it no longer reopens unseen when the widget comes back.

- A click on a taskbar widget's slot while its popover is open closes the popover again.

- A taskbar widget's open popover closes on a click into a window's page as well as on one elsewhere in the shell.

- On a phone, a widget's menu no longer offers Collapse or Expand, which the phone's always-collapsed strip ignored.

- Showing or hiding a widget for a name no app can have answers 404, as for an unknown app, rather than failing with a server error.

- Fixed: pressing the desktop switcher or the presence strip now closes an open tray widget popover, and a widget page's right-click on a phone no longer opens a menu that only appeared later, back on the desktop.
