import { defineConfig } from "vite";
import path from "path";

// The browser-side modules every app serves from its own origin, each as its own library
// build: the app contract (contracts.md section 10), one ES module with no other imports, at
// /_static/app_contract.js, and the element context menu (the element-reference-menu plan),
// which bundles the reference and row modules and nothing else, at /_static/context_menu.js
// (see SHELL_APP_CONTRACT_PATH and SHELL_CONTEXT_MENU_PATH in app_manifest.registry). Separate
// from the main build because a multi-entry app build shares chunks and would give the served
// files imports; the two entries here share no module, so each bundles whole. Runs AFTER the
// main build, whose emptyOutDir would otherwise delete this output.
export default defineConfig({
  build: {
    outDir: path.resolve(__dirname, "../imbue/system_interface/static/_static"),
    emptyOutDir: false,
    lib: {
      entry: {
        app_contract: path.resolve(__dirname, "../../../libs/workspace_ui/src/app_contract.ts"),
        context_menu: path.resolve(__dirname, "../../../libs/workspace_ui/src/context_menu.ts"),
      },
      formats: ["es"],
      fileName: (_format, entryName) => `${entryName}.js`,
    },
    minify: false,
  },
});
