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
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
