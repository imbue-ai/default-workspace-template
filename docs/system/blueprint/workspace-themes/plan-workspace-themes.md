# Workspace themes

This is the spec for themes: whole looks the workspace can wear, of which the standard look is one.
It defines what a theme is (a folder of files, section 3), the contract between the interface and a theme (tokens, parts, and window chrome, section 4), how a theme reaches every app page (section 5), how a theme's icons are made with any image model (section 6), how far each app takes part (section 7), and how a user makes a theme of their own (section 8).
It is written for the people and agents implementing and extending it, and it is the reference the implementation is judged against.
Classic Mac (on system.css) and Windows 2000 (on win95.css) ship as theme folders like any other.

## 1. Principles

- **A theme is data, not code.** A theme is a folder of CSS, assets, and a manifest. It carries no JavaScript, so a theme can change how everything looks and nothing about how anything behaves. What a theme may change about layout it declares in its manifest, and the interface renders the declaration (section 4.3).
- **One contract, versioned.** The interface promises a set of tokens, parts, and chrome slots (section 4), numbered as `contract = 1`. A theme styles only through that contract. Nothing else in the markup is a theme's to touch: a class name is an app's private business and can change in any release.
- **Themes are loaded, not compiled in.** No app build contains a theme. Every app page loads the one theme it wears at run time, from its own origin, so a theme a user made a minute ago works in every app without a rebuild.
- **Every theme has an icon guide.** Icons are part of a look, so a theme says, in prose and in checkable limits, how its icons are drawn, and the workspace can draw a missing one with whatever image model it can reach.
- **The standard look is a theme too.** It is the base every other theme starts from, and it obeys the same rules.

## 2. Glossary

| Term | Meaning |
|---|---|
| Theme | A folder under `system/themes/` (built in) or `themes/` (made in this workspace) with a `theme.toml` |
| Theme id | The folder's name: `^[a-z0-9][a-z0-9-]{0,47}$` |
| Base | The theme a theme starts from; every value it does not set comes from its base, recursively, ending at `standard` |
| Token | A CSS custom property the interface reads (colors, type, radii, bevels, the desktop's metrics) |
| Part | A named element of the interface, marked `data-part="<name>"`, that a theme may style |
| App part | A part an app declares in its own manifest, named `<app>.<part>` |
| Chrome | The window furniture the shell draws: the title bar and its controls |
| Theming mode | How far an app takes part: `tokens`, `parts`, or `none` (section 7) |
| Bundle | What a page loads for a theme: the theme's style files and its bases', in order (section 5.1) |

## 3. The theme folder

```
<root>/<id>/
  theme.toml            the manifest (section 3.1); required
  *.css                 the style files the manifest lists, in its order
  apps/<app>.css        styles for an app's declared parts; optional
  fonts/, assets/       what the style files reference with url(); optional
  vendor/               a third-party library adapted to the contract (section 4.5); optional
  icons/guide.md        the icon guide (section 6.1); required when the manifest declares [icons]
  icons/<app>.<format>  curated icons, one per app; optional
  icons/app.<format>    the generic program icon; required when the manifest declares [icons], except on standard
  LICENSE*              licenses of anything vendored
```

Roots are scanned in this order: `system/themes/` (the built-in themes, shipped with the template), then `themes/` (themes made in this workspace, committed like any other creation, so a template can carry them).
A theme folder may not be a link, nor contain one. `theme.css` at the top of the folder is the bundle's name (section 5.1), so no file of the theme may take it.
An id appears once across both roots; a workspace theme with a built-in theme's id is refused with an error that names both folders.
`standard` is built in and reserved.

Files a theme may contain: `.toml`, `.md`, `.json` and `.txt` (a vendoring map, notes), `.css`, `.woff2`, `.woff`, `.ttf`, `.otf`, `.png`, `.gif`, `.bmp`, `.webp`, `.jpg`, `.svg`, and license files (any name starting with `LICENSE` or `COPYING`).
A theme folder is at most 16 MB, and its style files together at most 2 MB.

### 3.1 `theme.toml`

```toml
contract = 1
id = "windows-2000"
name = "Windows 2000"
description = "Beveled gray windows with the navy caption gradient, Tahoma type, and a blue desktop, on win95.css."
base = "standard"

[styles]
files = ["vendor/win95.css", "tokens.css", "parts.css"]
apps = ["chat"]

[chrome]
title_align = "start"
leading = ["icon", "title", "refresh", "menu"]
trailing = ["minimize", "maximize", "close"]

[icons]
guide = "icons/guide.md"
format = "png"
size = 32
rendering = "pixelated"
background = "transparent"
max_colors = 48
derive = "quantize"
references = ["icons/files.png", "icons/chat.png", "icons/terminal.png"]

[[attribution]]
name = "win95.css"
url = "https://github.com/AlexBSoft/win95.css"
license = "MIT"
files = ["vendor/win95.css", "vendor/assets", "vendor/LICENSE-win95"]
```

| Key | Type | Required | Rule |
|---|---|---|---|
| `contract` | integer | yes | The contract version the theme is written to. This release reads `1`; another value makes the theme unavailable, with that reason. |
| `id` | string | yes | Equals the folder's name. |
| `name` | string | yes | 1 to 48 characters; what the theme picker shows. |
| `description` | string | yes | 1 to 200 characters; one sentence. |
| `base` | string | no | Defaults to `standard`; absent on `standard` itself. Must name an available theme; the chain, from `standard` to the theme itself, holds at most 4 themes and has no cycle. |
| `author` | string | no | Free text, at most 80 characters. |
| `styles.files` | list of paths | no | Style files, in the order they apply. Defaults to empty. |
| `styles.apps` | list of app names | no | Apps this theme styles beyond tokens; each has `apps/<name>.css` (section 7). |
| `chrome` | table | no | Section 4.3; every key defaults to its base's value. |
| `icons` | table | no | Section 6.1; absent takes the base's. Required on `standard`. |
| `attribution` | list of tables | no | One per third-party work in the folder: `name`, `url`, `license` (an SPDX id or "free font license"), `files` (paths or folders it covers). Required for anything the theme did not write. |

Paths are relative to the theme folder, use `/`, and may not leave it (no `..`, no absolute paths).
Unknown keys are errors: a typo is a mistake to report, not a value to ignore.

### 3.2 Style files

Plain CSS, as a browser reads it: no preprocessor, no `@import` (the bundle orders the files, section 5.1), no `@layer` (a theme's rules must stay unlayered, section 5.1).

- **Selectors** use only what the contract names: element names, `:root`, `html`, `body`, pseudo-classes and pseudo-elements, and attribute selectors. A class selector (`.x`), an id selector (`#x`), and an attribute selector on `class`, `id`, or `style` are errors: they reach into an app's private markup.
- **`data-part` values** are contract parts (section 4.2) or app parts the app declares (section 7). Any other value is an error.
- **Custom properties** a theme sets are contract tokens (section 4.1) or its own, named `--theme-*`. Setting any other custom property is an error.
- **`url()`** is a relative path to a file in the folder, or a `data:` URI. A remote URL is an error. A URL is written out: a function that takes one (`url()`, `src()`, `image-set()`) may not take `var()`, `env()`, or `attr()`, and a URL may not contain a backslash.
- **Fonts** are declared with `@font-face` in the theme's own files, with sources inside the folder.

## 4. The contract (`contract = 1`)

### 4.1 Tokens

A theme sets tokens on `:root` (and under `:root[data-touch]` or `:root[data-phone]` where a value should differ by render mode).

| Group | Tokens |
|---|---|
| Colors | every `--c-*` in `system/libs/workspace_ui/src/base.css` |
| Type | `--font-sans`, `--font-mono`, `--font-display` (headings, section labels, and what a theme sets its titles, buttons, and menus in), `--font-size-heading-lg`, `--font-size-heading`, `--font-size-body`, `--font-size-row`, `--font-size-helper`, `--weight-regular`, `--weight-semibold`, `--weight-bold` |
| Shape | `--radius-sm`, `--radius-md`, `--radius-lg`, `--radius-xl` |
| Motion | `--dur-fast`, `--dur-base`, `--dur-slow` |
| Desktop | `--desk-window-radius`, `--desk-window-shadow`, `--desk-taskbar-surface`, `--desk-taskbar-entry-shadow`, `--desk-backdrop`, `--desk-default-wallpaper`, `--desk-icon-radius`, `--desk-icon-shadow`, `--desk-icon-shadow-lifted`, `--desk-shortcut-label-shadow`, `--desk-toast-radius`, `--desk-phone-sheet-radius`, `--desk-phone-tile-radius` |
| Desktop metrics | `--desk-title-bar-height` (16 to 64 px), `--desk-taskbar-height` (24 to 96 px), `--desk-window-control-size` (12 to 48 px), `--desk-taskbar-entry-size` (16 to 64 px) |
| Terminal | `--term-background`, `--term-foreground`, `--term-cursor`, `--term-selection`, `--term-ansi-0` to `--term-ansi-15` |

The metric tokens are read by the shell's geometry (`theme/metrics.ts`) when the theme changes, and the validator refuses a value outside its range or in a unit other than `px`.
Every other desktop token the shell declares (the grid, the snap and drag thresholds, the resize handles) belongs to behaviour and is not a theme's to set.
The standard theme's values are the ones `base.css` and the shell's `theme/default.css` declare; they are compiled into every app, so a page with no theme loaded wears the standard look.

### 4.2 Parts

Every part carries `data-part`. State is said with standard attributes, which a theme selects on: `:disabled`, `[aria-disabled="true"]`, `[aria-pressed="true"]`, `[aria-expanded="true"]`, `[aria-selected="true"]`, `[aria-current="true"]`, and the parts' own attributes below.

| Part | What it is | Attributes |
|---|---|---|
| `desktop` | the desktop's backdrop | |
| `window` | one window, chrome and content | `data-focused="true"\|"false"`, `data-window-state` |
| `window-frame` | the window's border box; transparent where the page shows through | |
| `title-bar` | the window's title bar | `data-focused` |
| `window-icon` | the app's icon in the title bar | |
| `window-title` | the window's title | |
| `window-control` | a title bar button | `data-control="close"\|"minimize"\|"maximize"\|"restore"\|"refresh"\|"menu"` |
| `window-content` | the box the app's page is laid over | |
| `taskbar` | the bar along the bottom of the desktop | |
| `taskbar-entry` | a window's entry in the taskbar | `data-focused` |
| `launcher-field` | the taskbar's field that opens apps and sends messages | |
| `shortcut` | a desktop icon | |
| `shortcut-label` | a desktop icon's name | |
| `app-icon` | an app's icon wherever the shell draws one | `data-icon-source="standard"\|"theme"\|"derived"\|"fallback"` |
| `button` | a push button | `data-variant="primary"\|"secondary"\|"ghost"\|"destructive"\|"ghost-destructive"\|"inverse"\|"ghost-inverse"\|"stop"`, `data-shape="text"\|"icon"` |
| `field` | a text input or text area | |
| `select` | a value picker's trigger | |
| `menu` | a floating menu | |
| `menu-item` | a row in a menu | |
| `menu-separator` | a rule between menu rows | |
| `dialog` | a modal dialog's card | |
| `dialog-header` | its header row | |
| `dialog-title` | its title | |
| `dialog-actions` | its button row | |
| `tooltip` | a hover tooltip | |
| `badge` | a small status label | |
| `tile` | a card that opens something (Getting Started's tiles, a picker's choices) | `aria-pressed` where it is a choice |
| `list-row` | a selectable row in a list | `aria-current` on the current one |

The list lives in code as `PARTS` in `system/libs/workspace_ui/src/themes/parts.ts`, and in the validator's copy in `system/libs/workspace_themes`; a test keeps the two equal, and the theme gallery (section 8.2) renders every one.

### 4.3 Chrome

The title bar is drawn from two ordered slot lists.

| Key | Values | Standard |
|---|---|---|
| `title_align` | `"start"` or `"center"` | `"start"` |
| `leading` | slots | `["icon", "title", "refresh", "menu"]` |
| `trailing` | slots | `["minimize", "maximize", "close"]` |

The slots are `icon`, `title`, `refresh`, `menu`, `minimize`, `maximize` (drawn as restore while the window is maximized), and `close`.
Every slot but `icon` appears exactly once across the two lists, and `icon` at most once: a theme rearranges the window's controls, and never takes one away.
With `title_align = "center"` the title is centred on the whole bar, whichever list holds it.
A slot the window does not show (refresh and the window menu on a window in the background, maximize on a phone) leaves no gap.
Spacing between controls, their size, and their look are the theme's CSS.

### 4.4 Render modes

The shell marks `:root` with `data-touch` and `data-phone` while those modes are on, and with `data-ui-theme="<id>"` for the theme it wears.
A theme may key rules on all three.

### 4.5 Third-party libraries

A theme may be built on a CSS library (system.css, win95.css, 98.css, ...).
The library is vendored into the theme's `vendor/` folder by a script that pins the source by version or commit and digest, maps the library's selectors onto parts (`.btn` to `[data-part="button"]`, `.title-bar` to `[data-part="title-bar"]`, ...), and drops every rule whose selector names a class it does not map.
The adapted file is ordinary contract CSS and obeys section 3.2.
The markup never carries a library's class names.
`system/libs/workspace_themes/scripts/vendor-css-library.mjs` is the script, and each built-in theme's mapping is a JSON file beside its vendored output.

## 5. Delivery

### 5.1 The bundle

Every app that wears themes serves `GET /_static/themes/<path>` from its own origin, through the shared handler in `workspace_themes` (a stylesheet's fonts must come from the page's own origin, and the desktop client's forwarder refuses a cross-origin fetch).

- `/_static/themes/<id>/theme.css` is the bundle: generated `@import` rules for each style file of each theme in the chain, base first, then the theme's own `styles.files` in order, then its `apps/<app>.css` files. Every app overlay is in every bundle: its selectors name its own app's parts, so it matches nothing on another app's page.
- `/_static/themes/<id>/<file>` is a file in the theme folder, refused unless it is a style file or something one loads (`.css`, a font, an image). The manifest, the guides, and the vendoring maps stay on disk.
- `standard` has no style files; its bundle is empty.
- An unknown or unavailable theme answers `404`.
- Every answer carries `Cache-Control: no-cache` and a validator (an `ETag`, and `Last-Modified` on a file), so an edited theme shows on the next load.
- Every answer carries `X-Content-Type-Options: nosniff`; an svg also carries `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`. The shell's icon route answers the same way.

A theme's rules are unlayered and the bundle loads after the app's own stylesheet, so a theme's rule beats every Tailwind utility (`@layer utilities`) and, at equal specificity, the app's own unlayered rules.

### 5.2 Choosing and storing the theme

The workspace has a default theme, and a desktop may wear another.

`desktop_themes.json` in the shell's state folder: `{"version": 1, "default": "<id>" | null, "desktops": {"<desktop id>": "<id>"}}`.
A null default is `standard`.
A desktop missing from `desktops` wears the default.
A missing or invalid file reads as no themes chosen.
The theme a desktop wears is its own, else the default, else `standard`; one that is not available (deleted, or failing validation) is worn as `standard`, and the picker says so.

| Route | Request | Response |
|---|---|---|
| `GET /api/themes` | | `200 {"default", "themes": [theme]}` |
| `POST /api/themes/default` | `{"theme": "<id>" \| null}` | `200 {"default"}`; `400` for an unavailable theme |
| `POST /api/desktops/<id>/theme` | `{"theme": "<id>" \| null}` | `200 desktop`; null makes the desktop wear the default; `400` for an unavailable theme; `404` for an unknown desktop |
| `GET /api/themes/<id>/icons/<file>` | | the icon file, from the theme folder or the workspace's generated icons (section 6.3) |

`theme` is `{"id", "name", "description", "base", "source": "builtin" \| "workspace", "available", "problems": [string], "revision", "chrome", "icons"}`.
`revision` changes whenever a file in the theme's chain changes.
`chrome` is the resolved section 4.3 table.
`icons` is the resolved section 6.1 table with `fallback_url` and `apps` (app name to icon URL, for the apps that have a curated or generated icon).
`problems` lists every validation error; a theme with any is unavailable.
The desktop record carries `theme`, its own choice or null.

### 5.3 Pages

The shell tells each page it frames which theme to wear with `shell:theme` `{"theme": "<id>", "revision": "<revision>", "isPreview": <bool>}`, after the handshake and again on every change; `isPreview` is true while Desktop settings shows a choice that is not saved yet.
`system/libs/workspace_ui/src/themes/themeClient.ts` is a page's side:

- `wearTheme(theme)` (a `{id, revision}`) puts one `<link rel="stylesheet" data-workspace-theme>` at the end of `<head>` (render-blocking when the theme is put on while `<head>` is read, as the boot script and the page kit do) pointing at `/_static/themes/<id>/theme.css?v=<revision>` (none for `standard`), sets `data-ui-theme` on `<html>`, and remembers the choice in the origin's local storage under `workspace-theme`, unless the shell marks it a preview.
- Before the page's first paint, a small inline script (injected by the `themeBoot()` Vite plugin into every app's `index.html`) wears the remembered theme, so a page opens in its theme rather than flashing the standard look.
- A page framed by a page of its own app (the chat's inner pages) mirrors its parent and never remembers the theme itself: the parent, on the same origin, does.
- A new stylesheet replaces the old one only once it has loaded, so a switch never shows an unstyled frame.
- A page built as plain HTML rather than on the shared library loads the **page kit** the shell builds and every app serves beside its app contract: `/_static/workspace_theme.css` (the standard tokens, and the standard look of the `button`, `field`, `tile`, `list-row`, and `badge` parts on plain elements) and then `/_static/workspace_theme.js` (the boot and `shell:theme` handling above, as one classic script). The `build-app` scaffold puts both in every new app's `<head>`.

When a theme's files change, a theme folder is added or removed, or `POST /api/themes/default` sets the workspace's default, the shell sends `themes_changed` `{"catalog"}` (the `GET /api/themes` document) on its WebSocket; the desktop takes the new catalog, and a changed theme or revision reaches every page as a new `shell:theme`.

The shell previews a theme by wearing it on its own page without remembering it, and sending it to the pages it frames with `isPreview: true`, which wear it without remembering it either (Desktop settings shows a theme the moment it is picked). Save sends it again with `isPreview: false`; Cancel puts the saved one back.

## 6. Icons

### 6.1 The icon guide

Every theme says how its icons look, twice: in prose for people and models (`icons/guide.md`), and as limits a script can check (`[icons]`).

| Key | Type | Required | Rule |
|---|---|---|---|
| `guide` | path | yes | A Markdown guide: subject treatment, outline, perspective, light, palette, what to avoid, and how the reference icons show it. |
| `format` | `"svg"` or `"png"` | yes | |
| `size` | integer | yes | The canvas, 16 to 512 px square. For `png`, the image's exact size. |
| `rendering` | `"smooth"` or `"pixelated"` | yes | How the shell scales the icon. |
| `background` | `"transparent"` or `"opaque"` | yes | Whether the corners must be clear. |
| `palette` | list of `#rrggbb` | no | When not empty, every opaque pixel is one of these colors. |
| `max_colors` | integer | no | At most this many distinct opaque colors (2 to 256). |
| `derive` | `"none"`, `"pixelate"`, `"quantize"`, or `"monochrome"` | yes | How the shell makes an icon from an app's standard one when the theme has none (section 6.3). |
| `references` | list of paths | no | Icons of this theme that show the style best; given to a model as examples. |

The standard theme's guide is `docs/system/app-icons.md` (the 216-pixel two-layer tile with one fill-only glyph), which `system/themes/standard/icons/guide.md` points to; its icons are each app's own `icon.svg`.

### 6.2 Making an icon, with any model

The `make-theme-icon` skill makes a theme's icon for an app:

1. It reads the theme's guide and limits, the app's name, display name, and purpose, and the app's standard icon.
2. It draws a candidate with whatever it can reach: an image tool the harness provides, an image API the user has a key for (the skill's `providers/` has examples), or, with no image model at all, a pixel grid or an SVG the agent writes itself.
3. `workspace-themes icon fit` forces the candidate onto the limits: trims it, scales it to the canvas (nearest neighbour for a pixelated theme), quantizes it to the palette or color count, and clears or fills the background.
4. `workspace-themes icon check` checks the result. A png icon: size, format, background, palette, color count, that it is not blank, and that it still reads scaled down to 16 pixels (enough contrast between the icon and its background). An svg icon: that it is well-formed SVG with a `viewBox`, so it scales, and holds no script, event handler, `foreignObject`, or reference outside itself. `workspace-themes validate` holds a theme's own svg icons to the same rules.
5. `workspace-themes icon install` puts it in place (section 6.3).

The fitting step is what lets any model's output join a set drawn by another.

### 6.3 Where icons live and which one is drawn

- A theme's curated icons are in its folder: `icons/<app>.<format>`.
- Icons generated in a workspace for a built-in theme are in `data/.themes/<id>/icons/<app>.<format>`, so the built-in theme folder stays as shipped. A workspace theme's generated icons go into its own folder.

Under a theme, the shell draws an app's icon as the first of: the theme's curated icon, the workspace's generated icon, an icon derived from the app's standard icon by the theme's `derive` (rendered in the page: rasterized to the canvas, then pixelated, quantized to the palette, or turned to one color), and the theme's generic program icon (`derive = "none"`).
The `app-icon` part's `data-icon-source` says which it drew.

When an app is built (the `build-app` skill) and when a theme is made (the `create-theme` skill), the agent makes the missing icons in the background; the derived icon shows until they land, and neither is finished until every theme with icons of its own has one for every app.
`workspace-themes icon missing [<id>...]` lists, for each such theme (every available one when none is named), the apps it has no icon for, and exits 1 when there are any; a theme that keeps the standard icons has none to draw.
Every built-in theme with icons of its own ships, committed in its folder, an icon for every built-in app a user sees; a test reads the app manifests, so a new built-in app fails it until each such theme has one.

## 7. Apps

An app says in its manifest how far it takes part:

```toml
[theming]
mode = "parts"
parts = [
    { name = "user-message", description = "A message the user sent" },
    { name = "composer", description = "The box a message is typed in" },
]
```

| Mode | What it means |
|---|---|
| `tokens` (default) | The app is built only from the shared components and the design tokens. Every theme styles it with no work from the theme. Its frontend uses no literal colors, font families, or shadows; `workspace-themes lint-app <app package>` (its folder under `system/apps/`) checks. |
| `parts` | The app has a look of its own somewhere (chat bubbles, a canvas). It marks those elements `data-part="<app>.<name>"` and declares them; a theme may style them in `apps/<app>.css`. A theme that does not still styles the rest through tokens. |
| `none` | The app shows content whose look is not the workspace's (another site, a terminal's output). The shell themes the window around it; the page itself is left alone. |

New apps use `tokens`. An app reaches for `parts` only for a look the shared components cannot give, and keeps its declared parts stable: renaming one breaks every theme that styles it, so it is a contract change, made with a note in the app's changelog.
A theme lists the apps it styles in `styles.apps`, and the validator checks that every app part it names is declared by that app.
An overlay for an app the workspace does not have is left out of the bundle and not checked, so a theme made for one workspace still works in another.

## 8. Making themes

### 8.1 The `create-theme` skill

A user starts one in a chat, in their own words ("make it look like BeOS"), or from **Make your own...** at the end of Desktop settings' Theme row, which closes the dialog and drafts a theme request, unsent, into the pinned chat window, the way the avatar chooser's "Design your own..." does; with no window on the desktop that takes a draft, the row says to describe the look in a chat instead.

The agent makes a theme from a description or a picture:

1. `workspace-themes new <id> --base <base> --name "<name>"` lays out the folder.
2. The agent writes the tokens, the chrome slots, the part styles, and the icon guide, drawing on a CSS library where one fits (section 4.5).
3. It makes the icons (section 6.2) in the background, until `workspace-themes icon missing <id>` names no app.
4. `workspace-themes validate <id>` checks the folder against sections 3 and 4.
5. It looks at the theme gallery under the theme, and fixes what reads badly.
6. It shows the user the theme with the live preview in Desktop settings.

### 8.2 The theme gallery

The shell serves `/theme-gallery?theme=<id>`: every part in every state, the chrome of a focused and a background window, dialogs, menus, fields, buttons, tiles, list rows, and every app's icon.
It is how a person or an agent judges a theme, and what the browser tests load to check every built-in theme: the theme's bundle loads, no page script errors, and the window frame and content stay transparent over the page beneath.

## 9. Implementation map

| Piece | Where |
|---|---|
| Theme folders | `system/themes/standard/`, `system/themes/mac-classic/`, `system/themes/windows-2000/` |
| Manifest models, discovery, validation, serving, icon tools, CLI | `system/libs/workspace_themes/` (`workspace-themes`) |
| Library vendoring | `system/libs/workspace_themes/scripts/vendor-css-library.mjs` |
| Parts list, theme client, boot plugin | `system/libs/workspace_ui/src/themes/` |
| Theme storage and routes | the shell's `desktops.py` (storage) and `theme_routes.py` |
| Chrome slots, icons and their derivation, settings | the shell frontend's `TitleBar.ts`, `glyphs.ts`, `themeIcons.ts`, `DesktopSettingsDialog.ts` |
| The gallery | the shell frontend's `gallery/` entry |
| App theming levels | `app_manifest`'s `AppTheming`, each app's `app.toml` |
| Skills | `.agents/skills/create-theme/`, `.agents/skills/make-theme-icon/`, and the `build-app` skill's theming step |
