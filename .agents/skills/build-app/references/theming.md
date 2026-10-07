# Building an app that wears the workspace's theme

The user can dress their workspace in a theme (Desktop settings > Theme): the standard look,
Classic Mac, Windows 2000, or one they made. Every app's window is themed by the shell; what
happens *inside* the window depends on how the app is built. The design is
`docs/system/blueprint/workspace-themes/plan-workspace-themes.md` (section 7 for apps).

An app declares how far it takes part, in `app.toml`:

```toml
[theming]
mode = "tokens"
```

| Mode | When |
|---|---|
| `tokens` | The default, and what the scaffold writes. The page is built from the page kit's parts and the design tokens, so every theme dresses it with no work from the app or the theme. |
| `parts` | The app has a look of its own somewhere that the kit's parts cannot give (a chat bubble, a board's cards). It marks those elements `data-part="<app>.<name>"` and declares them under `[theming] parts`, so a theme may style them. Everything else still follows the tokens. |
| `none` | The page shows content whose look is not the workspace's: another site, a document viewer, a terminal's output. The window around it is themed; the page is left alone. |

Choose `tokens` unless the app clearly needs one of the others. Ask the user only if they want
the app to keep a fixed look under every theme (that is `none`).

## Building a `tokens` page

The scaffold already loads the page kit in the page's `<head>`:

```html
<link rel="stylesheet" href="/_static/workspace_theme.css" />
<script src="/_static/workspace_theme.js"></script>
```

The stylesheet is the standard look; the script puts the theme the user wears after it, before
the first paint, and follows the shell when the user changes theme. Keep the stylesheet first in
`<head>`, and the script after your own styles: the theme's stylesheet goes in where the script
runs, and it must come after every style of the page's to win over them.

Then build every control from a part, and every color, font, radius, and shadow from a token:

- Buttons: `<button data-part="button" data-variant="primary">` (`primary`, `secondary`,
  `ghost`, `destructive`; no variant is a plain button).
- Text fields and text areas: `data-part="field"`.
- A card that opens something: `data-part="tile"`. A selectable row: `data-part="list-row"`,
  with `aria-current="true"` on the current one. A small label: `data-part="badge"`.
- Colors: `var(--c-bg)`, `var(--c-surface)`, `var(--c-text-primary)`,
  `var(--c-text-secondary)`, `var(--c-border)`, `var(--c-accent)`, `var(--c-danger)`, ... (the
  full list is at the top of `/_static/workspace_theme.css`).
- Type: `var(--font-sans)` for text, `var(--font-display)` for headings, `var(--font-mono)`
  for code, and the `--font-size-*` and `--weight-*` steps.
- Shape: `var(--radius-sm|md|lg|xl)`, `var(--c-shadow-raised)`, `var(--c-shadow-overlay)`.

Never write a literal color, font family, or shadow in the page's CSS or markup: a literal is a
spot every theme misses. `frontend-choices.md`'s defaults are these tokens.

Check before calling the page done:

```bash
uv run workspace-themes lint-app <package>
```

It lists every literal it finds, by file and line, in the app's frontend sources and in its Python modules
(where the scaffold's page markup lives); it skips tests and built output (`static/`, `dist/`, `build/`,
`assets/`).

An app built on the shared TypeScript library (`system/libs/workspace_ui/`, the way the chat
app is) gets the same from the library's components; add the library's `themeBoot()` Vite
plugin and pass `connectToShell`'s `onTheme` on to `wearThemeFromMessage`, preview flag included
(`onTheme: (theme, revision, isPreview) => void wearThemeFromMessage({ theme, revision, isPreview })`), as the
chat app does.

## Looking at it under each theme

Look at the page under every installed theme before showing it, privately, with Playwright
(`uv run workspace-themes list` names them). The page wears the theme it finds remembered for
its origin, so set that before the page loads:

```python
page.add_init_script(
    "localStorage.setItem('workspace-theme', JSON.stringify({id: 'windows-2000', revision: 'preview'}))"
)
page.goto(app_url)
page.screenshot(path="/tmp/<app>-windows-2000.png")
```

Fix text that does not contrast, a control that does not look like one, or anything that kept
the standard look (a literal the lint missed, an element with no part).

## Icons under a theme

Under Classic Mac or Windows 2000 the shell draws an icon derived from the app's standard one
until the theme has its own. When the app is finished, make them in the background with the
`make-theme-icon` skill: `uv run workspace-themes icon spec <theme>`, run for each theme, lists
the apps that theme still has no icon for.
