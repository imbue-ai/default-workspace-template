New apps must now register an icon: `forward_port.py` refuses a registration that would create a brand-new pickable entry without `--icon`/`--icon-file`, so an app can no longer silently fall back to the generic letter-in-a-box monogram. Re-registrations of existing entries (the supervisord-restart case, and apps that predate the rule) are unaffected and keep their monogram.

A new `--no-icon` flag opts out explicitly, for machinery whose entry is hidden from the app pickers. The built-in `system_interface`, `browser`, and `terminal` registrations (which have their own UI glyphs and never render a registry icon) now pass it.

`--icon-file` also now refuses non-`.svg` files with a clear error naming SVG as the only supported format, so a raster icon fails with guidance instead of an XML parse error.
