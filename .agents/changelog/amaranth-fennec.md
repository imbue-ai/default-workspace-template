The icon a new app is asked for is a tile now, not line art.

- `build-app` describes the icon it requires by pointing at `docs/system/app-icons.md` -- a 48x48 two-layer tile, a flat background under one fill-only glyph in a centred 32x32 box, coloured from the pair palette that doc carries -- in place of the monochrome `currentColor` line-art frame it used to spell out. Both the pre-flight step and the `forward_port.py` CLI reference say so, so an agent scaffolding an app draws the same thing the built-in apps wear.
