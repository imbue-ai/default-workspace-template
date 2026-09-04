import { defineConfig } from "vite";
import path from "path";

// The browser-side app contract (contracts.md section 10) as its own library build: one
// ES module with no other imports, which the shell serves at /_static/app_contract.js for
// any app origin to load. Separate from the main build because a multi-entry app build
// shares chunks and would give the served file imports. Runs AFTER the main build, whose
// emptyOutDir would otherwise delete this output.
export default defineConfig({
  build: {
    outDir: path.resolve(__dirname, "../imbue/system_interface/static/_static"),
    emptyOutDir: false,
    lib: {
      entry: path.resolve(__dirname, "src/app_contract.ts"),
      formats: ["es"],
      fileName: () => "app_contract.js",
    },
    minify: false,
  },
});
