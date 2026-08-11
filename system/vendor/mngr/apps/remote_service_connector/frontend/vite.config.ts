import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";

// Builds the hosted accounts pages (login / signup / manage) into dist/,
// which `minds env deploy` attaches to the connector's Modal image (served
// by accounts_web.py). `base` makes every asset URL resolve under
// /accounts/assets/ -- the lazily-resolved asset route -- while index.html
// itself is served for each page path.
export default defineConfig({
  plugins: [tailwindcss()],
  base: "/accounts/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
