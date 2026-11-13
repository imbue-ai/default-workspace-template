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
icons under `system/themes/<theme>/icons/` were drawn for the workspace, most with
Retro Diffusion and the Routines icons by hand.

## Atkinson Hyperlegible Next, Atkinson Hyperlegible Mono, and Besley

The workspace's type is three open fonts, copied into
`system/libs/workspace_ui/src/fonts/` and served by every app from its own
origin: Atkinson Hyperlegible Next and Atkinson Hyperlegible Mono (the Braille
Institute's faces, by The Atkinson Hyperlegible Next Project Authors and The
Atkinson Hyperlegible Mono Project Authors), and Besley's Medium weight (by The
Besley Project Authors), which the shared stylesheet names `Besley Display`.
All three are under the SIL Open Font License 1.1. Each licence sits in
`licenses/` as its project publishes it: `atkinson-hyperlegible-next-OFL.txt`
and `atkinson-hyperlegible-mono-OFL.txt` from the Google Fonts repository, and
`besley-OFL.txt` from the Besley project.

## The dictation's model and detector

The chat's dictation (`system/libs/workspace_voice/`) runs two models on the
user's own device. Neither is copied into this tree: the speech model is
downloaded by the page from Hugging Face at a pinned commit and kept in the
browser's own storage for the workspace, and the detector's model ships with
the chat's build out of the npm package that carries it.

- Moonshine Base, by Useful Sensors (<https://github.com/usefulsensors/moonshine>),
  MIT-licensed, as exported to ONNX at
  <https://huggingface.co/onnx-community/moonshine-base-ONNX>; the registry's
  entry (`system/libs/workspace_voice/src/voice_models.json`) names the commit
  and the licence.
- Silero VAD, by Silero (<https://github.com/snakers4/silero-vad>), MIT-licensed,
  as `@ricky0123/vad-web` bundles it (its own licence is in that package).
