Hovering a window behind the focused one and scrolling now scrolls its page, with no click first. A click or press on a background window reaches its page and brings the window to the front in the same press, as in a native desktop application; before, a transparent cover took that first press and only raised the window. Keys typed after the press go to the window that came forward.

The shell raises a window whenever the document's focus lands in its page, so this holds for every app, whether or not it speaks the shell contract; a page that focuses itself while its window is behind brings it forward too.

The focus requests the shell posts to a window it raises, which a terminal takes into its pty, are dropped as soon as another window becomes the focused one, so raising a window within a second of a terminal no longer lets a late request bring the terminal back to the front.

Touch screens keep the cover over background windows, so the first tap there still only raises the window, and every window is covered while a menu or the launcher is open, so the press that closes them never reaches a page. A viewed desktop stays read-only.
