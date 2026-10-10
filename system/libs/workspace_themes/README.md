# workspace_themes

Workspace themes as data: the models of a theme folder, the catalog that finds and resolves the workspace's themes, the
validator that holds each theme to the contract, the handler every app uses to serve the themes from its own origin,
and the tools an agent uses to make and check a theme's icons with any image model. The spec is
`docs/system/blueprint/workspace-themes/plan-workspace-themes.md`; the theme folders are `system/themes/` (built in)
and `themes/` (made in the workspace).

## API

- `workspace_themes.data_types`: `ThemeManifest` (a `theme.toml`, `extra = "forbid"`), `ThemeCatalog` and
  `ThemeEntry` (a theme resolved through its bases: its chain, style files, title bar, icons, revision, and the
  problems that make it unavailable).
- `workspace_themes.catalog`: `CachingThemeCatalogLoader(repo_root=...)`, whose `load()` reads the catalog again only
  when a theme file, a generated icon, or an app manifest changed; `read_theme_catalog` for explicit roots.
- `workspace_themes.validation`: `check_style_sheet` and the folder, icon, and chrome checks the catalog runs.
- `workspace_themes.serving`: `resolve_theme_asset` (the bundle and the files it loads), `resolve_theme_icon`, and
  `theme_file_security_headers(path)`, the headers every answer with a theme file carries (`nosniff`, and a
  script-free `Content-Security-Policy` on an svg).
- `workspace_themes.flask_routes`: `register_workspace_theme_route(app)` serves `/_static/themes/<path>` from the
  app's origin, reading the themes from the working directory (the repo root every supervised app runs from); it is
  the one call an app makes. `register_theme_static_route(app, load_catalog)` serves the same route from a
  `Callable[[], ThemeCatalog]` the caller supplies, as the shell does with its own loader.
- `workspace_themes.icons`: `fit_icon_image`, `check_icon_image`, `render_pixel_grid`, `icon_install_path`.
- `workspace_themes.lint`: `lint_app_frontend`, the literal-look finder for tokens-mode apps.
- `workspace_themes.contract`: the parts, tokens, chrome slots, and limits of contract 1.

## CLI

```bash
uv run workspace-themes list
uv run workspace-themes validate [<id> ...]
uv run workspace-themes new <id> --name "<name>" --description "<one sentence>" [--base <id>]
uv run workspace-themes bundle <id>
uv run workspace-themes icon spec <theme>
uv run workspace-themes icon fit <candidate.png> --theme <theme> --out <fitted.png>
uv run workspace-themes icon grid <grid.txt> --theme <theme> --legend "k=#000000,w=#ffffff" --out <icon.png>
uv run workspace-themes icon check <icon.png> --theme <theme>
uv run workspace-themes icon install <icon.png> --theme <theme> --app <app>
uv run workspace-themes lint-app <app package>
```
