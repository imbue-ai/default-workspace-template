- The show-files-in-chat skill links files (and folders) by absolute path or `file:///` URL so a click opens them in the File Viewer, and links local services by their localhost URL, which opens a registered app as its own window.

- The build-app skill says a scaffolded app's links are followed by the app contract (plain `<a href>` links, no link handling of its own), and tells a wrapped third-party tool that can serve a static file and add a script to its pages to load the contract too.
