The shared UI library takes part in workspace themes (`docs/system/blueprint/workspace-themes/plan-workspace-themes.md`).

- Its components carry the contract's parts (`data-part`), so a theme styles them without depending on class names.

- New theme client (`src/themes/themeClient.ts`), which loads the theme a page wears from the page's own origin, and boot plugin (`src/themes/themeBoot.ts`), which wears the remembered theme before first paint.

- New page kit (`src/page/`), a stylesheet and a script every app serves at `/_static/workspace_theme.css` and `/_static/workspace_theme.js`, so a page built as plain HTML wears the workspace's theme too. On such a page a plain `<button>`, text `<input>`, `<textarea>`, or `<select>` wears the theme as its part does, with no marking; `data-variant` on a plain button still gives it emphasis.

- The app contract's `shell:theme` message (`{theme, revision, isPreview}`) reaches an app's `onTheme(theme, revision, isPreview)`, and `followShellTheme` follows the theme without connecting.
