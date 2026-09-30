Taskbar and launcher refresh.

- A taskbar entry is a labelled chip: the app's icon with the window's title
  beside it at body size. The chip tints under the pointer and for the window
  you are in; a window that is out of sight -- minimized, or shown in a window
  of the chrome's own -- dims until the pointer reaches it, and the hover bubble
  carries the whole of a title the bar had to cut short.

- The bar itself is solid, with no line along its top and no blur behind it, and
  everything in it sits in one element that can be dressed on its own.

- Icons in the bar and on the desktop carry a cast of their own and a hairline of
  relief: light along the top edge, shade along the bottom, so a flat tile reads
  as a face rather than a sticker on any colour the palette offers.

- The launcher field wears a plus rather than a magnifier, asks to "Open an app
  or send a message", takes body text, and is rounded to match the card above
  it; that card now takes the field's width and the chrome's menu styling.

- An app that declares no launch path of its own is offered by its name rather
  than as "Open <name>", so a verb in that list means a different kind of row.

- The Desktops tray widget is parked behind a single grid button: switching
  desktops, the desktops menu and its settings dialog are out of view while how
  they should come back is decided. Everything behind them is intact, and the
  end-to-end tests drive desktops over the shell's API in the meantime.
