- The show-files-in-chat skill links files (and folders) by absolute path or `file:///` URL so a click opens them in the File Viewer, links a registered app by the `link` `workspace-layout list` prints for it (its share address once the workspace has been shared, so the link opens for anyone it is shared with; else its localhost URL), and other local services by their localhost URL.

- The build-app skill's public URL reference says where an app's share address comes from (`workspace-layout list`) instead of telling agents to ask the user for it.

- The build-app skill says a scaffolded app's links are followed by the app contract (plain `<a href>` links, no link handling of its own), and tells a wrapped third-party tool that can serve a static file and add a script to its pages to load the contract too.
