Two small workspace-UI polish fixes:

- The project picker dropdown now anchors its horizontals on the rail card itself (and the menu placement's left floor no longer pushes a menu right of its own anchor), so the picker's left border sits flush with the rail panel's border instead of 2px inside it.

- The four "Open new" tiles on the New Tab screen (Chat, File viewer, Browser, Terminal) now carry hover tooltips, reusing the rail shortcuts' copy for the same four kinds. The unbacked file viewer keeps its "coming soon" explanation, and all tooltips still drop while a create is in flight.

- The rail now sits 1px lower, so its header row's visible top edge -- the project picker bar -- lands exactly on the tab chips' top edge instead of 1px above it, and the divider under the header lines up with the pane card's top border.
