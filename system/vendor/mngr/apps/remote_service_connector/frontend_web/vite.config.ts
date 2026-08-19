import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";

// Builds the hosted minds web chrome into dist/, which `minds env deploy`
// attaches to the connector's Modal image (served path-routed under /web by
// accounts_web.py). `base` makes every asset URL resolve under /web/assets/
// (the lazily-resolved asset route) while index.html itself is served for
// every SPA page path.
export default defineConfig({
  plugins: [tailwindcss()],
  base: "/web/",
  // Bakes the deploy id into the bundle as its X-Imbue-Client build stamp
  // ("web/<deploy-id>"), directly comparable to the connector's /version
  // deploy_id. "dev" outside a `minds env deploy` build.
  define: {
    __MINDS_DEPLOY_ID__: JSON.stringify(process.env.MINDS_DEPLOY_ID ?? "dev"),
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Emit .map files next to the bundles so production stack traces resolve
    // back to the TypeScript source. They land in dist/assets/, ride onto the
    // Modal image with the rest of dist, and are served by the existing
    // /web/assets/ route. Note the maps embed the original source
    // (sourcesContent), which is publicly fetchable from that route.
    sourcemap: true,
  },
});
