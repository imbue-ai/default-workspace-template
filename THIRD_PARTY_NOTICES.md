# Third-party notices

The workspace's own code carries its own terms: the shell app under
`system/apps/system_interface/LICENSE`, and each vendored subtree under
`system/vendor/` its own `LICENSE`. The built-in themes' adapted copies
of system.css and win95.css, with the images those packages ship (not their fonts, which the themes leave out), sit
under `system/themes/<theme>/vendor/` with each package's licence beside them,
and each theme's `theme.toml` names them under `[[attribution]]`. This file carries the notices that third-party material
copied into the tree asks for.

Bundled dependencies are not listed here. A dependency installed from npm or
PyPI keeps its own licence inside its package, and a build that reproduces one
in its output reproduces the notice with it (tailwindcss stamps its banner into
every stylesheet it emits, for instance).

## Lucide, and Feather through it

Several of the chrome's stroke icons are Lucide glyphs, copied as inline SVG
path data rather than imported as a package: they live in
`system/libs/workspace_ui/src/components/icons.ts` and
`system/apps/system_interface/frontend/src/views/glyphs.ts`, drawn on Lucide's
own 24x24 frame.

Lucide's licence sits in `licenses/lucide-LICENSE`, copied byte for byte from
<https://github.com/lucide-icons/lucide/blob/main/LICENSE> -- the file
<https://lucide.dev/license> publishes. It has two parts: Lucide's own drawings
are ISC-licensed, and the ones it names as derived from Feather are MIT-licensed
by Cole Bemis. Glyphs copied here come from both sides of that line, so both
parts apply. If Lucide changes the file, take their copy again rather than
editing this one -- a notice restated in someone else's words stops being the
notice.

The app icons are not Lucide's: every one is drawn for this workspace to the
rules in `docs/system/app-icons.md`, and the Classic Mac and Windows 2000 themes' pixel-art
icons under `system/themes/<theme>/icons/` were drawn for the workspace with
Retro Diffusion.
