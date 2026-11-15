import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { configDefaults } from "vitest/config";
import path from "path";

export default defineConfig({
  // ``dist/`` is never this project's output (the bundle goes to ``build.outDir``); a stale one left by an
  // older build would otherwise have vitest collect the compiled copy of every test beside its source.
  test: {
    exclude: [...configDefaults.exclude, "dist/**"],
    environment: "jsdom",
  },
  plugins: [tailwindcss()],
  root: ".",
  build: {
    // Inside the Python package, where the service serves it from.
    outDir: path.resolve(__dirname, "../src/screen/static"),
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": { target: "http://localhost:8090", ws: true },
      "/neko": { target: "http://localhost:8090", ws: true },
      "/health": { target: "http://localhost:8090" },
    },
  },
});
