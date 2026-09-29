The rules every app icon is drawn to, and the docs and scripts that point at them.

- `docs/system/app-icons.md` is new, and is now the one statement of what an app icon is: 48x48, two layers, a flat background under one fill-only glyph in a centred 32x32 box, a 32 per cent corner radius, and a colour pair from the palette it carries. It also says what the file may and may not hold (no shadow, no strokes, a root `fill="none"`, a clip only for a glyph drawn to the tile's edge), how a glyph is drawn and how an existing one is changed without losing its hand-placed points, and what the workspace draws behind one (nothing) -- and it says outright that this look is the workspace's default rather than a fixed law, with this doc as the place to change it.

- `AGENTS.md` and `forward_port.py` point at that doc rather than describing a house style of their own: the registration error an app with no icon raises names it, and the module's own docs say the markup check here is about safety, not style.

- The file viewer's icon is Courage on Inspiration, drawn on Gleb's Copyboard and built to those rules, replacing its `currentColor` line glyph.

- `test_app_manifests.py` pins the order of the built-ins the desktop seeds its shortcuts in and the launcher lists -- chat 10, Getting Started 15, the file viewer 20, the browser 30, the terminal 40 -- rather than asserting one app's rank on its own.
