import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { configDefaults } from "vitest/config";
import path from "path";

// dufs serves the build from its `--assets` directory, flat, under a prefix of its own (`/__dufs_v<version>__/`) that
// it fills into index.html's `__ASSETS_PREFIX__`. It reads index.html once, at startup, and serves the rest with a
// year-long immutable cache. So the bundle keeps fixed names -- a dufs still holding an older index.html then serves
// the new bundle rather than a name a rebuild deleted -- and the page asks for it with this build's stamp, which is
// what moves a browser past its cached copy.
const DUFS_ASSETS_PREFIX = "__ASSETS_PREFIX__";
const BUILD_STAMP = Date.now().toString(36);

export default defineConfig({
  test: {
    exclude: [...configDefaults.exclude, "dist/**"],
  },
  plugins: [tailwindcss()],
  root: ".",
  experimental: {
    renderBuiltUrl(filename, { hostType }) {
      if (hostType === "html") return `${DUFS_ASSETS_PREFIX}${filename}?v=${BUILD_STAMP}`;
      return { relative: true };
    },
  },
  build: {
    // The directory the `files` program line hands dufs as `--assets`.
    outDir: path.resolve(__dirname, "../static"),
    emptyOutDir: true,
    assetsDir: "",
    rollupOptions: {
      output: {
        entryFileNames: "index.js",
        chunkFileNames: "[name].js",
        assetFileNames: "[name][extname]",
      },
    },
  },
});
