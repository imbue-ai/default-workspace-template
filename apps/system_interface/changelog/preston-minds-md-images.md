Agents can now show images inline in chat. An agent writes an image file and references its absolute on-disk path with normal markdown image syntax (`![alt](/mngr/code/runtime/chat-images/chart.png)`); the system interface serves the file at that path so it renders inline, with no upload step.

The new handler hangs off the single-page-app catch-all: a request whose path carries an image extension (`.png`, `.jpg`/`.jpeg`, `.gif`, `.webp`, `.svg`) is served from disk when the file exists, or returns a 404 (broken image) when it does not, so a typo'd path never silently renders the app shell. Non-image paths fall through to the SPA exactly as before, so client-side routing is unaffected.

Images are served inline with an immutable, one-year cache (agents are instructed to use unique filenames per image), and SVGs are additionally served with a restrictive `Content-Security-Policy` and `X-Content-Type-Options: nosniff` so a directly-opened SVG cannot execute scripts.
