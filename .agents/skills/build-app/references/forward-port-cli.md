# `forward_port.py` CLI reference

`forward_port.py` registers an app's port in `data/.state/apps.toml`, which is
what gives the app its origin and its place in the launcher. Both paths use it:
the scaffolder generates the call, and a wrapped server's program has you write
it. Read this when you write or change a registration by hand.

```
python3 system/scripts/forward_port.py --manifest system/apps/<package>/app.toml --url URL
python3 system/scripts/forward_port.py --name NAME --url URL --icon-file PATH
python3 system/scripts/forward_port.py --name NAME --remove
```

The script is standard-library only and runs under a plain `python3`.

## Flags

- `--manifest`: the app's `app.toml`. This is the form every app with a
  directory uses. Its `name` (validated like `--name` below), the icon file it
  names (validated like `--icon-file`), and its static fields (`display_name`,
  `critical`, `priority`, `program`, `internal`, `launcher_rank`,
  `default_shortcut`, `launch_paths`, `pin`) are copied onto the registry row
  on every call. `--name` may accompany it and must then equal the manifest's
  name.
  `--icon-file`, `--program`, `--internal` and `--no-icon` are for
  registrations with no app directory (previews, isolated test servers) and
  cannot be combined with it.
- `--name`: the app's name. It seeds the hostname label of the app's origin
  (`public-url.md`), so it is validated, and registration fails loudly on an
  invalid name. The rule is step 2's naming rule in SKILL.md, loosened in three
  ways: underscores are accepted (for built-in names such as
  `system_interface`), a name may start with a digit, and a name another
  program already uses is not checked.
- `--url`: the full URL where the app is reachable from inside the container
  (`http://localhost:8090`).
- `--icon-file`: the path to the app's `.svg` icon (SVG only, no rasters),
  drawn instead of the generic letter monogram. **Required when creating a new
  entry** (unless `--internal` or `--no-icon`); omitting it on re-registration
  keeps the stored icon. The file's *contents* are stored: a single safe
  `<svg>` element (no script, style, event handlers, or external references;
  at most 16384 characters). A bad file fails a new registration loudly, but
  only warns on re-registration (the stored icon is kept), so a corrupted icon
  cannot crash-loop a running app. How the icon is drawn, and how an existing
  icon is changed without losing its hand-placed points, is
  `docs/system/app-icons.md`.
- `--no-icon`: skip the icon requirement for a brand-new entry, which then
  uses the generic letter monogram. Use it only when the user explicitly
  declines an icon, or for short-lived preview windows.
- `--program`: the name of the supervisord program that runs the app. Both
  paths name the program after the app, so pass the app's own name. Its
  presence on the registry entry is what lets the shell act on the app's
  process over supervisord's RPC: Quit it from its window menu, stop it once no
  window shows it, hold its port while it is stopped, and start it again on the
  next request (the `/api/apps/<name>/stop` and `/start` routes remain for
  agents). Omitting it clears any previously stored value, so every
  registration call is authoritative. Never pass it for unsupervised instances
  (previews, `serve_isolated_instance.py` test servers): those own their own
  teardown and must not offer a Stop that supervisord cannot honor.
- `--remove`: remove the named entry from `data/.state/apps.toml`. Use it when
  tearing an app down (`cleanup.md`).
