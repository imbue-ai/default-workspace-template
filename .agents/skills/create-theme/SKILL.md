---
name: create-theme
description: Use when the user wants a new look for their workspace -- a theme made from a description ("make it look like BeOS", "a dark solarized theme"), a picture, or an existing theme to start from. Lays out a theme folder, writes its colors, type, window chrome, and part styles against the theme contract, checks it, judges it in the theme gallery, makes its icons, and lets the user try it in Desktop settings.
metadata:
  author: imbue
---

# Making a workspace theme

A theme is a folder of CSS and a `theme.toml`, with no script in it. It sets the workspace's
design tokens (colors, type, radii, metrics), styles the interface's **parts** (buttons, fields,
menus, title bars, windows, ...), arranges the title bar's controls, and says how its icons
look. Every app built from the shared components wears it with no work of its own. The
contract -- every token, part, and rule the validator holds a theme to -- is
`docs/system/blueprint/workspace-themes/plan-workspace-themes.md`; read sections 3 and 4
before writing any CSS.

The built-in themes under `system/themes/` are the worked examples: `standard` (the default
look, almost empty), `mac-classic`, and `windows-2000` (both built on a vendored CSS library).
Themes made here go in `themes/<id>/` at the repo root, and are committed like any creation.

Run every command from the repo root.

## 1. Settle the look

From the user's description or picture, decide, before writing anything:

- the palette: page, surfaces, text, borders, accent, danger -- as the `--c-*` tokens name them;
- the type: body face, display face (titles, buttons, menus), mono face; only faces the user's
  machine has, or a font file shipped in the theme's `fonts/` under a license that allows it;
- the shape: corner radii, borders, bevels or shadows, how a pressed button looks;
- the title bar: which controls sit left and right, and whether the title is centred;
- the icons: what an icon looks like in this style (it becomes the icon guide).

For a look the user named after a real system, ask nothing more: draw on what that system
looked like. Ask only when the description leaves the look genuinely open.

## 2. Lay out the folder

```bash
uv run workspace-themes new <id> --name "<Name>" --description "<one sentence>" --base <base>
```

`<id>` is lowercase with hyphens. Start from `standard` unless the new theme is a variation on
another (a dark Windows 2000 is `--base windows-2000`); a theme inherits every token, part
style, chrome setting, and icon its base has, and sets only what differs.

## 3. Write it

- **`tokens.css`**: the tokens under `:root`. Only contract tokens (section 4.1), plus private
  ones named `--theme-*` for values the part styles share. Metric tokens stay inside their
  ranges.
- **`parts.css`**: the part styles, selected only as `[data-part="..."]`, combined with the
  parts' own attributes (`data-variant`, `data-focused`, `data-control`, ... as section 4.2
  lists them), `aria-*`, pseudo-classes, and `:root[data-ui-theme]`,
  `[data-touch]`, `[data-phone]`. Never a class, an id, or `style`. Every part name is in the
  contract's list (section 4.2). No `@import`, no remote `url()`.
- **`theme.toml`**: add `[chrome]` (section 4.3) when the controls move, and `[icons]`
  (section 6.1) with `icons/guide.md` when the icons look different from the base's. A theme
  with its own `[icons]` also needs `icons/app.<format>`, the generic program icon.
- **`apps/<app>.css`**, optional: styles for parts an app declares in its `app.toml`
  (`[theming] mode = "parts"`; chat declares `chat.user-message`, `chat.composer`,
  `chat.tool-call`). List each such file under `[styles] apps`.
- **A CSS library**, when one exists for the look (98.css, 7.css, XP.css, ...): vendor it
  rather than redraw it. Write `vendor/<library>.vendor.json` -- the pinned source and its
  sha256, and a `selectorMap` from the library's classes to parts -- then run
  `node libs/workspace_themes/scripts/vendor-css-library.mjs ../themes/<id>/vendor/<library>.vendor.json`
  from `system/` (after `npm ci` there). The built-in themes'
  `system/themes/*/vendor/*.vendor.json` are the examples. Record the library under
  `[[attribution]]` with its license file.

Write the icon guide (`icons/guide.md`) as the built-in themes do: canvas and palette, outline,
view and light, what one object per app looks like, what to avoid, and which reference icons
show the style.

## 4. Check it

```bash
uv run workspace-themes validate <id>
```

It prints `ok` or every problem with its file. Fix each, and run it again until it is clean. A
theme with a problem is listed in Desktop settings as unavailable and cannot be chosen.

## 5. Judge it in the gallery

The shell serves `/theme-gallery?theme=<id>`: every part in every state, a focused and a
background window's chrome, a dialog, a menu, fields, buttons, tiles, list rows, and every
app's icon. Load it with Playwright (privately, not as a window on the user's screen),
screenshot it, and look hard: text that does not contrast with its surface, a control that
does not look pressable, a title bar whose controls collide, a field you cannot see the edge
of. Fix what reads badly and look again. The page lists any problem with the theme at its top.

The gallery checks that a window's frame and content stay clear over the app beneath; a theme
that paints them hides the app.

## 6. Make the icons

Until the theme has its own icons, the shell derives one per app from the app's standard icon
(`derive` in `[icons]`). For each app in `uv run workspace-themes icon spec <id>`'s
`apps_without_icon`, make one with the `make-theme-icon` skill -- in the background (the
`launch-task` skill) when there are more than one or two.

## 7. Show the user

Tell the user the theme is ready, and that they can try it in **Desktop settings > Theme**,
which previews it live before they keep it, and can put it on one desktop or make it the
workspace default, which every desktop without a theme of its own wears.
Do not switch their desktop to it yourself unless they asked you to.
