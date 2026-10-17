import { defineConfig } from "vite";
import path from "path";

// The page kit every app serves from its own origin beside the app contract (docs/system/blueprint/workspace-themes/,
// section 5.3): /_static/workspace_theme.js, a classic script that wears the workspace's theme (it runs in <head>,
// before the first paint, so it cannot be a module), and /_static/workspace_theme.css, the standard look for a page
// built as plain HTML. Its own build because the contract's entries are ES modules. Runs AFTER the main build, whose
// emptyOutDir would otherwise delete this output.
export default defineConfig({
  build: {
    outDir: path.resolve(__dirname, "../imbue/system_interface/static/_static"),
    emptyOutDir: false,
    lib: {
      entry: path.resolve(__dirname, "../../../libs/workspace_ui/src/page/workspace_theme.ts"),
      name: "workspaceTheme",
      formats: ["iife"],
      fileName: () => "workspace_theme.js",
      cssFileName: "workspace_theme",
    },
    minify: false,
  },
});
