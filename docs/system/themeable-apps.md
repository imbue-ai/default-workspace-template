# Making an app themeable

The user can dress the workspace in a theme (Desktop settings > Theme): the standard look,
Classic Mac, Windows 2000, or one they made. A themeable app wears it the way the workspace's own
apps do: its page loads the page kit, which puts the theme's stylesheet on the page and follows
the theme the shell names. The user turns an app's theming on and off from its window menu
(Theming, On or Off); while it is off, the shell names the standard theme to that app's pages,
which puts nothing on them. The design is
`docs/system/blueprint/workspace-themes/plan-workspace-themes.md`, section 5.3.

Making an app themeable means writing its look so every theme reaches it, while it keeps the look
it has today when theming is off. A literal color or font the app sets is one no theme can change.

## Mark the app themeable

Add to the app's `app.toml`:

```toml
[theming]
mode = "outside"
```

That is what gives its Theming submenu On and Off. Without it the submenu offers Enable theming
instead, and the shell treats the app as not wearing the theme.

## Serve the page kit and the themes from the app's own origin

The page reaches both on its own origin, never the shell's:

- `/_static/workspace_theme.js`, the page kit's script, which the shell builds at
  `system/apps/system_interface/imbue/system_interface/static/_static/workspace_theme.js`. An app
  the `build-app` skill made already serves the shell's modules from that folder: add
  `"workspace_theme.js"` to its `SHELL_STATIC_MODULE_NAMES`.
- `/_static/themes/`, the theme files the kit loads: call
  `register_workspace_theme_route(app)` from `workspace_themes.flask_routes`, and add
  `**THEME_STATIC_ROUTE_TABLE` from the same module to the app's `ROUTE_TABLE`.

Then load the kit in the page's `<head>`, after the app's own stylesheet:

```html
<script src="/_static/workspace_theme.js"></script>
```

Load only the script, not the kit's `workspace_theme.css`: that stylesheet gives the page the
workspace's standard look in place of the app's own.

## Put the app's own look in a cascade layer

Wrap the app's stylesheet in a layer:

```css
@layer app {
  /* everything the app's stylesheet held */
}
```

A theme's rules are unlayered, so wherever a theme sets something it beats the app, whatever the
selectors' weight, and everywhere else the app's own look stays. With theming off there is no
theme on the page, and the app looks as it always did.

## Take every color, font, radius, and shadow from a token

Replace each literal in the app's CSS, markup, and scripts with the matching token, written with
today's value as the fallback, so the app looks as it does now when no theme is on the page:

```css
.card { background: var(--c-surface, #ffffff); color: var(--c-text-primary, #2a2522); }
```

- Colors: every `--c-*` in `system/libs/workspace_ui/src/base.css`: `--c-bg`, `--c-surface`,
  `--c-text-primary`, `--c-text-secondary`, `--c-border`, `--c-accent`, `--c-danger`, and the
  rest there.
- Type: `--font-sans` for text, `--font-display` for headings, `--font-mono` for code, and the
  `--font-size-*` and `--weight-*` steps.
- Shape: `--radius-sm`, `--radius-md`, `--radius-lg`, `--radius-xl`, and the `--c-shadow-*`
  shadows.

Pick each token by what the value means, not by matching colors: the app's own accent is
`--c-accent`, its page background `--c-bg`. Never copy the current theme's values into the app;
the point is that the app changes when the theme does.

Never define a workspace token yourself (no `--c-*`, `--font-*`, or `--radius-*` declared in the
app's own CSS, on `:root` or anywhere else). The theme defines them, and a definition of the app's
own would stand wherever the theme's does not reach, so the app would look the same under every
theme it does not fully cover. The fallback inside `var()` is the only place today's value belongs.

An app that defines its own variables (`--accent: #d26645;`) can keep them, defined from the
tokens: `--accent: var(--c-accent, #d26645);`.

## Keep controls plain

The theme dresses a `<button>`, a text `<input>`, a `<textarea>`, and a `<select>` that carry no
`data-part` of their own: the kit marks the page, and the theme's look for its button, field, and
select parts reaches the bare controls. Give a control that should look like the workspace's its
look from the app's layer like any other element. For emphasis, add `data-variant="primary"` (or
`ghost`, `destructive`); a button that is only an icon is `data-shape="icon"`.

A control that keeps a look of its own on purpose (a big colored call-to-action, a glyph-only
delete) can say so with an inline `style` or an unlayered rule of its own; it then takes the theme
only through the tokens it uses.

## Check it

```bash
uv run workspace-themes lint-app <package>
```

lists every literal color, font family, and shadow still in the app's sources, by file and line.
Fix each, or leave it only where the app keeps a fixed look on purpose.

Then show the user the app in its window, and tell them they can switch themes in Desktop
settings > Theme, and turn the app's theming off and on from its window menu (Theming), while
they look.

## Ask about the app's icons

The reply that shows the user the app ends with this question, next to asking whether to keep the
change; never skip it. Ask whether they also want the app's icon drawn for each theme that
has an icon style of its own (`uv run workspace-themes icon missing` names those themes and the
apps they have no icon for). Say plainly that each icon takes time to draw and costs tokens, and
that without one the app keeps its standard icon, or one derived from it, under those themes.
Draw them only when they say yes and the change is kept, with the `make-theme-icon` skill, in the background.
