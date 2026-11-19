A newly built app's own tests now load the shell's page scripts.

- `build-app`'s scaffolded runner finds the shell's two browser modules (`/_static/app_contract.js` and `/_static/context_menu.js`) from the checkout its own source sits in. Before, it looked for them relative to the working directory, which is the repo root only for the running service: the app's tests run from the app's own directory, so every browser test there got a 404 for both modules and never exercised the page's connection to the shell.
