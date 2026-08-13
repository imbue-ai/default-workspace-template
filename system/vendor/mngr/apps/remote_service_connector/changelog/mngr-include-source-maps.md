# Ship JS source maps with the hosted minds web client

The web-chrome bundle (`frontend_web`, the hosted minds web client served under `/web`) now builds with `build.sourcemap: true`, so production JavaScript errors resolve back to the original TypeScript source in browser DevTools instead of dead-ending in minified stack frames. The `.map` files land in `dist/assets/` alongside the bundles, ride onto the connector's Modal image with the rest of the dist directory, and are served by the existing `/web/assets/` route with no server changes.

Note: the maps embed the original source (`sourcesContent`) and are publicly fetchable from the unauthenticated asset route, the same way the bundle itself is.
