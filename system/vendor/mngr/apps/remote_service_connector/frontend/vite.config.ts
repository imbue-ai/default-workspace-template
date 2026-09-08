import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";

// Builds the hosted accounts pages (login / signup / manage) into dist/,
// which `minds env deploy` attaches to the connector's Modal image (served
// by accounts_web.py). `base` makes every asset URL resolve under
// /accounts/assets/ -- the lazily-resolved asset route -- while index.html
// itself is served for each page path.
export default defineConfig({
  // Bakes the deploy id into the bundle as its X-Imbue-Client build stamp
  // ("web/<deploy-id>"), directly comparable to the connector's /version
  // deploy_id. "dev" outside a `minds env deploy` build.
  define: {
    __MINDS_DEPLOY_ID__: JSON.stringify(process.env.MINDS_DEPLOY_ID ?? "dev"),
  },
  plugins: [tailwindcss()],
  base: "/accounts/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
