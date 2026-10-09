- The show-files-in-chat skill links files (and folders) by absolute path or `file:///` URL, so a click opens them in the File Viewer.

- AGENTS.md says to link a registered app by the `link` `workspace-layout list` prints for it, and other local services by their localhost URL.

- The build-app skill's public URL reference says where an app's share address comes from (`workspace-layout list`) instead of telling agents to ask the user for it.

- The build-app skill says a scaffolded app's links are followed by the app contract (plain `<a href>` links, no link handling of its own), that its `window.open` to a workspace address is routed the same way and returns `null`, and tells a wrapped third-party tool that can serve a static file and add a script to its pages to load the contract too.

- The manage-desktop skill says `workspace-layout list` gives each app the `link` to write for it.
