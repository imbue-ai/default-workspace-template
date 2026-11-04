An app the workspace built can be made themeable, and its theming turned on and off by the user.

- The window menu's Theming submenu offers On and Off for a themeable app (`[theming] mode = "outside"`), with theming on by default. While it is off, the shell sends that app's pages the standard theme, so they keep the app's own look; turning it on or off sends every open page of the app its theme again.

- For an app that is not themeable yet, On and Off are greyed and "Enable theming" drafts a request into the main chat to make it themeable, following `docs/system/themeable-apps.md`.

- `POST /api/apps/<app>/theming` `{"is_on"}` turns a themeable app's theming on or off, kept in `app_theming.json`. The inventory carries each app's `theming`: `on`, `off`, `unthemed`, or null for an app that wears the theme itself or is not themed.
