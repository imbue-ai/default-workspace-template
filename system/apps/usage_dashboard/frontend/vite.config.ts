import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { configDefaults } from "vitest/config";
import path from "path";

export default defineConfig({
  // ``dist/`` is never this project's output (the bundle goes to ``build.outDir``); a stale one left by an
  // older build would otherwise have vitest collect the compiled copy of every test beside its source.
  test: {
    exclude: [...configDefaults.exclude, "dist/**"],
    // A zone with daylight saving, so the local-day tests cross a 23- and a 25-hour day.
    env: { TZ: "America/New_York" },
  },
  plugins: [tailwindcss()],
  root: ".",
  build: {
    // Inside the Python package, where the app serves it from.
    outDir: path.resolve(__dirname, "../src/usage_dashboard/static"),
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": { target: "http://localhost:8060" },
    },
  },
});
