`app_manifest.failure_pages` holds the pages an app's server writes whole when it cannot show what was asked for.

- `not_built_page_response(display_name, build, health_path)` is every app's page while its frontend is not built: "<name> isn't available right now. It comes back on its own once it is rebuilt.", a Reload, a check of the health route that reloads once the build is there, the build command behind "See details", and, inside a shell's window, "Ask an agent to rebuild it" with the contract module's remedies. `WORKSPACE_FRONTEND_BUILD` is the built-in apps' build command.

- `failure_remedies_script` mounts the contract module's `mountFailureRemedies` for any such page, from the module's route or from a copy the page carries.

- `testing.assert_serves_the_not_built_page` checks an app serves that page under its own name.
