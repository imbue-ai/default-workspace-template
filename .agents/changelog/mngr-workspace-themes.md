Skills for workspace themes.

- New `create-theme` skill: makes a workspace theme from a description or a picture -- lays out the folder, writes its tokens, chrome, and part styles (vendoring a CSS library where one fits), validates it, judges it in the theme gallery, and points the user at Desktop settings to try it; it then asks whether to draw new icons in the theme's style for every app, and draws them in the background if the user wants them.

- New `make-theme-icon` skill: draws an app's icon for a theme with whatever is available -- the harness's image tool, Retro Diffusion, any litellm image model, or a pixel grid or SVG the agent writes itself -- then fits, checks, and installs it.

- `build-app`: new apps are scaffolded to wear the workspace's theme (the page kit in the page's head, the theme route, and `[theming] mode = "tokens"`), and `references/theming.md` says how to build a page from the shared parts and tokens, check it under every theme, and make its theme icons. The default fonts in `frontend-choices.md` are now the design tokens.

- Building an app now includes drawing its icon for every theme with icons of its own (`workspace-themes icon missing` must come back clean), checked again by the hardening worker; making a theme likewise includes an icon for every app. An agent drawing icons in a checkout of its own (a background worker) installs and checks them against the live workspace (`--repo-root /home/user/workspace`), since its own `data/` is not the one the shell reads.

- `system/libs/workspace_themes/`, which the shell, the chat, and the terminal run, takes `update-app`'s careful flow like `workspace_ui` and `workspace_layout` (`AGENTS.md`, `update-app`, `update-self`, and the harden-contention reference say so).
