The rules every app icon is drawn to, and the docs and scripts that point at them.

- `docs/system/app-icons.md` is new, and is now the one statement of what an app icon is: 48x48, two layers, a flat background under one fill-only glyph in a centred 32x32 box, a 32 per cent corner radius, a colour pair from Gleb's contrast board (every approved pair and its contrast ratio is tabled there), and what the file may and may not carry -- no shadow, no strokes, a root `fill="none"`, a clip only for a glyph drawn to the tile's edge. It also says how a glyph gets out of Figma without losing its hand-placed points, and what the workspace draws behind one (nothing).

- `AGENTS.md` and `forward_port.py` point at that doc rather than describing a house style of their own: the registration error an app with no icon raises names it, and the module's own docs say the markup check here is about safety, not style.

- The file viewer's icon is Courage on Inspiration, drawn on Gleb's Copyboard and built to those rules, replacing its `currentColor` line glyph.
