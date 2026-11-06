Skills for workspace themes.

- New `create-theme` skill: makes a workspace theme from a description or a picture -- lays out the folder, writes its tokens, chrome, and part styles (vendoring a CSS library where one fits), validates it, judges it in the theme gallery, and points the user at Desktop settings to try it; it then asks two things about icons: whether to draw one in the theme for each app that has none, in the background, and whether apps built later should get one too, which makes each build cost more and take longer. Only a yes to the second tells app building about the theme, by adding one line naming it to the workspace's `AGENTS.md`; otherwise new apps use their standard icon under every theme.

- New `make-theme-icon` skill: draws an app's icon for a theme with whatever is available -- the harness's image tool, or a pixel grid or SVG the agent writes itself -- then fits, checks, and installs it.

- An icon in a theme's style is drawn only when the user asks for one; until then each theme shows an icon derived from the app's own. An agent drawing icons in a checkout of its own (a background worker) installs and checks them against the live workspace (`--repo-root /home/user/workspace`), since its own `data/` is not the one the shell reads.

- `system/libs/workspace_themes/`, which the shell, the chat, and the terminal run, takes `update-app`'s careful flow like `workspace_ui` and `workspace_layout` (`AGENTS.md`, `update-app`, `update-self`, and the harden-contention reference say so).
