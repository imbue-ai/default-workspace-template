Skills now call the desktop app Imbue Studio and the thing you talk to "your agent". The desktop app is now called Imbue Studio, and the noun for the thing you talk to is now "your agent" rather than "your mind".

The template skills carry most of this: publish-template, use-template, update-installed-template, update-published-template and migrate-workspace describe adopting, publishing and importing in the new vocabulary, and the README that publish-template generates for a published repo now says "a published Imbue Studio template" with an "Open in Imbue Studio" button, matching the badge the build script already emitted.

update-self's user-facing error text now names Imbue Studio ("could not reach the Imbue Studio app", "Update the Imbue Studio app itself first"), with its test assertions moved in lockstep.

The Caretaker introduces itself as "a Caretaker for your workspace" rather than for your agent: under the new vocabulary the Caretaker is itself an agent, and what it looks after is the workspace.

Nothing machine-readable moved. The `minds-api` skill keeps its directory name and gateway route, the `minds-template` GitHub topic and the `(minds template v2)` repo-description marker stay verbatim (both are read by the catalog generator and the publish flow), and `MINDS_*` environment variables, `~/.minds` paths, `minds-placeholder-thumbnail`, the betterleaks rule ids and the `minds-v*` release tags are untouched.
