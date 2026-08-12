Document the app-icon flags in the build-app skill.

`forward_port.py` now takes an optional `--icon` (SVG markup) or `--icon-file` (a path whose contents are read at registration time), so an app can register the glyph the workspace draws for it. The CLI reference lists both, along with what the validation accepts — a single `<svg>` element, no script, style, event handler or external reference, at most 16384 characters — and the rule that omitting them leaves an already-registered icon alone.

The public-URL reference no longer claims `apps.toml` holds only `name` and `url`; it holds the service `label` and any registered `icon` markup too, and still never a public URL.
