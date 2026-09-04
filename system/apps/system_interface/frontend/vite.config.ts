import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { configDefaults } from "vitest/config";
import path from "path";

export default defineConfig({
  // `dist/` is not part of this project's output -- the bundle goes to
  // `build.outDir` below, and nothing reads `dist/`. It exists only where a
  // checkout still has the emit an older `build` script left behind, and
  // vitest's default include would then collect the compiled COPY of every
  // test beside its source: the same suite twice, with one half frozen at
  // whenever that build ran. Excluded so a stale directory cannot quietly
  // double the count or report passes from code that is no longer there.
  //
  // Added TO vitest's own defaults rather than written out beside them:
  // `exclude` is a whole-list override, so a hand-copied list silently drops
  // whatever else vitest excludes by default and leaves this file owning a
  // decision it has no opinion about. `dist/**` is the only local one.
  test: {
    exclude: [...configDefaults.exclude, "dist/**"],
  },
  plugins: [tailwindcss()],
  publicDir: "media",
  root: ".",
  resolve: {
    alias: {
      // The minds embed contract -- the single sanctioned postMessage channel
      // between this UI and the embedding minds chrome -- is fetched from the
      // mngr commit pyproject.toml pins (system/scripts/fetch_mngr_assets.sh, run
      // by this package's prebuild) so both sides always ship from one source.
      // Types come from src/embed-contract.d.ts; keep the two in sync.
      "@minds/embed-contract": path.resolve(
        __dirname,
        "../../../vendor/mngr-assets/apps/minds/imbue/minds/desktop_client/static/embed_contract.js",
      ),
    },
  },
  build: {
    outDir: path.resolve(__dirname, "../imbue/system_interface/static"),
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
