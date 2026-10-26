import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { configDefaults } from "vitest/config";
import path from "path";
import { themeBoot } from "@imbue/workspace-ui/src/themes/themeBoot";

export default defineConfig({
  // ``dist/`` is never this project's output (the bundle goes to ``build.outDir``); a stale one left by an
  // older build would otherwise have vitest collect the compiled copy of every test beside its source.
  test: {
    exclude: [...configDefaults.exclude, "dist/**"],
  },
  // themeBoot wears the theme this origin last wore before the first paint (workspace-themes plan section 5.3).
  plugins: [tailwindcss(), themeBoot()],
  root: ".",
  build: {
    // Inside the Python package, where the app serves it from.
    outDir: path.resolve(__dirname, "../src/getting_started/static"),
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": { target: "http://localhost:8030" },
    },
  },
});
