A new bundled wallpaper, and an app's icon drawn as the tile it now is.

- A new bundled wallpaper, arcs, in a warm taupe colourway, and the default every desktop wears until it picks one of its own. Added beside dawn rather than over it, so a desktop that chose dawn keeps it and the picker offers both.

- A desktop shortcut paints nothing behind an app's icon. Every icon is a tile now (`docs/system/app-icons.md`), with its own background and its own 32 per cent corner, so the white surface and the 8px of padding the icon box used to carry -- there to make a transparent line glyph legible -- were framing each tile in a second one and drawing it at 32px inside a 48px box. The box keeps the matching rounding and `--desk-icon-shadow`, since the shadow and the hover are what it still owes the tile.

- An app that registered no icon wears its monogram on a tile too: its initial in Courage on Strength, the most neutral pair on the board, built to the same rules. With nothing painted behind it, the outlined `currentColor` letter it used to draw would have been left floating on the wallpaper.
