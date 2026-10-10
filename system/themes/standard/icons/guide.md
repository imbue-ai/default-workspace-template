# Standard icons

The standard theme's icon for an app is the app's own `icon.svg`, beside its `app.toml`, drawn to
`docs/system/app-icons.md`: a 216 by 216 two-layer tile with a 32 percent corner radius, one fill-only glyph in a
centred 144 by 144 box, its two colours a pair from that doc's palette, drawn by hand. Read that doc in full before
drawing one; it is the guide, and this file only points at it so every theme has its guide in the same place.

An app gets its standard icon when it is built (the `build-app` skill), not through `workspace-themes icon install`:
the registration script reads `icon.svg` into the registry.
