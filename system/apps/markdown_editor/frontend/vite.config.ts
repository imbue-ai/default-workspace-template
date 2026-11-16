import { defineConfig } from "vite";
import { configDefaults } from "vitest/config";
import path from "path";

export default defineConfig({
  // ``dist/`` is never this project's output (the bundle goes to ``build.outDir``); a stale one left by an
  // older build would otherwise have vitest collect the compiled copy of every test beside its source.
  test: {
    environment: "jsdom",
    exclude: [...configDefaults.exclude, "dist/**"],
  },
  root: ".",
  build: {
    // Inside the Python package, where the app serves it from.
    outDir: path.resolve(__dirname, "../src/markdown_editor_app/static"),
    emptyOutDir: true,
    // Tiptap and ProseMirror are one bundle of about a megabyte, loaded once per window.
    chunkSizeWarningLimit: 1500,
  },
  server: {
    proxy: {
      "/api": { target: "http://localhost:8070" },
    },
  },
});
