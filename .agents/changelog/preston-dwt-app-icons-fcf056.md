The build-app skill now requires an icon for every new app: the pre-flight checklist has the agent draw a purpose-specific glyph in the house style before scaffolding, the scaffolder takes a required `--icon-file` (validated up front with `forward_port.py`'s own rules, copied to `system/apps/<package>/icon.svg`, and registered on every supervisord start), and the escape-hatch path stores the icon at `system/apps/<name>/icon.svg` the way the built-in `files` app does. This pairs with `forward_port.py` now refusing brand-new registrations without an icon.

The isolated-instance/preview helper (`serve_isolated_instance.py`) registers its short-lived services with the new `--no-icon` flag, since previews are not pickable apps.

The scaffolder reuses `forward_port.py` for icon reading as well as validation, so its `--icon-file` likewise accepts only `.svg` files -- keeping every app icon in the same vector style.
