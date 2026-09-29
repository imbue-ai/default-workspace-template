The shared menu (`system/libs/workspace_ui`) gained two things the desktop's new size menu needed, both available to any menu:

- **Hover triggers** (`hoverTriggerAttrs`): a control that already does something on its click can offer a menu as the slower way in. It opens and closes on the pointer exactly as a submenu does, and lays no sheet under itself, so the chrome around the trigger stays hoverable; a press outside, Escape, and `close` still take it down.
- **`center` alignment** in `placeMenu`, on both axes, for a menu hanging off something too small to align an edge to.
