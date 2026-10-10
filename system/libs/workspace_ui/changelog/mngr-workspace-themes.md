Workspace themes become a real feature: a theme is a folder of CSS and a `theme.toml` against a versioned contract (`docs/system/blueprint/workspace-themes/plan-workspace-themes.md`), and Standard, Classic Mac, and Windows 2000 ship as the built-in themes under `system/themes/`. The workspace can also hold its own themes in `themes/`.

- The shared UI library marks its components with the contract's parts (`data-part`), so a theme styles them without depending on class names, and adds the theme client (`themeClient.ts`), the boot plugin that wears a theme before first paint (`themeBoot.ts`), and the page kit for plain-HTML pages (`src/page/`). The app contract's `shell:theme` carries `isPreview`, which `onTheme` receives as a third argument, and `followShellTheme` follows the theme without connecting.

- Each built-in theme carries an icon guide and limits; Classic Mac and Windows 2000 carry pixel-art icons for the built-in apps in `system/themes/<theme>/icons/`.

- The built-in themes ship no font files: Classic Mac sets its type in Chicago, Geneva, and Monaco where the machine has them, and in the nearest installed faces where it does not.

- Docs: the desktop-interface and workspace-app-model contracts, `docs/system/app-icons.md`, `AGENTS.md`, `THIRD_PARTY_NOTICES.md`, and the library README describe themes.
