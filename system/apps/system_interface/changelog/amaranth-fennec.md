A new bundled wallpaper, an app's icon drawn as the tile it now is, and a title bar that recedes behind the window you are in.

- A new bundled wallpaper, arcs, in a warm taupe colourway, and the default every desktop wears until it picks one of its own. Added beside dawn rather than over it, so a desktop that chose dawn keeps it and the picker offers both.

- A desktop shortcut paints nothing behind an app's icon. Every icon is a tile now (`docs/system/app-icons.md`), with its own background and its own 32 per cent corner, so the white surface and the 8px of padding the icon box used to carry -- there to make a transparent line glyph legible -- were framing each tile in a second one and drawing it at 32px inside a 48px box. The box keeps the matching rounding and `--desk-icon-shadow`, since the shadow and the hover are what it still owes the tile.

- An app that registered no icon wears its monogram on a tile too: its initial in Courage on Strength, the most neutral pair on the board, built to the same rules. With nothing painted behind it, the outlined `currentColor` letter it used to draw would have been left floating on the wallpaper.

- A window's title bar carries the app's icon at 20px rather than 14, starts 6px from the window's edge, and leaves 6px between the icon and the title. At a control's size the tile read as mud; 20 in a 36px bar leaves it 8px of clearance and makes it a picture again. The title is bold while the window is focused, and stays at medium when it is not -- the shade of grey that said which window you were in does not read from across the screen.

- An unfocused window's bar recedes further: its icon fades to half, its title to seven tenths, and the refresh and window-menu buttons beside the title are dropped rather than greyed, so a background window reads as a name and the controls every window owes you. The icon has to be faded by hand, since a tile that paints itself cannot take the bar's colour the way a line glyph did. The buttons return with the click that focuses the window, and nothing to their right moves when they do; the taskbar entry's own menu still reaches a window you are not in.
