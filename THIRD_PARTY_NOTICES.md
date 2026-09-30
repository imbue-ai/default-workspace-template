# Third-party notices

The workspace's own code carries its own terms: the shell app under
`system/apps/system_interface/LICENSE`, and each vendored subtree under
`system/vendor/` its own `LICENSE`. This file carries the notices that
third-party material copied into the tree asks for.

Bundled dependencies are not listed here. A dependency installed from npm or
PyPI keeps its own licence inside its package, and a build that reproduces one
in its output reproduces the notice with it (tailwindcss stamps its banner into
every stylesheet it emits, for instance).

## Lucide, and Feather through it

Several of the chrome's stroke icons are Lucide glyphs, copied as inline SVG
path data rather than imported as a package: they live in
`system/libs/workspace_ui/src/components/icons.ts` and
`system/apps/system_interface/frontend/src/views/glyphs.ts`, drawn on Lucide's
own 24x24 frame. Lucide is ISC-licensed, and its notice covers the Feather
icons it descends from:

```
ISC License

Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as part of Feather (MIT). All other copyright (c) for Lucide are held by Lucide Contributors 2022.

Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

The app icons are not Lucide's: every one is drawn for this workspace to the
rules in `docs/system/app-icons.md`.
