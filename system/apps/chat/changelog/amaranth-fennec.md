A new icon: the imbuman as a two-layer tile.

- The chat's icon is the imbue figure in Confidence on Comfort, drawn on Gleb's Copyboard and built to the rules in `docs/system/app-icons.md` -- a flat 48x48 background under one fill-only glyph, with the 32 per cent corner radius the shell's `--desk-icon-radius` computes. It replaces the `currentColor` line glyph, so the chat wears its own colours wherever the workspace draws it.

- It is the one icon in the set that carries a `clipPath`: the figure stands on the tile's bottom edge rather than inside the centred 32x32 box every other glyph fits, so the tile's rounded corners have to cut its feet.
