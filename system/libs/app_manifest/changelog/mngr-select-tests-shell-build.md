`select-tests` now builds the frontends (`npm ci && npm run build`) before the full root suite, and before any root-collected run of an app, when the tree lacks the shell's built modules (`_static/app_contract.js` and `_static/context_menu.js`).

Before, a harden worker's test gate in a fresh worktree ran the full root suite with no build. The shell's `static/` is gitignored, so it was missing there. An app's pages import those modules, so the browser tests of workspace-built apps failed after 30-second timeouts (pr-review's row-menu tests) or hung (a work-dashboard context-menu test), even though the change under test had nothing to do with them.

A tree that already has the modules, like the live workspace, gets no build line, since building would rewrite the bundles the running shell serves.
