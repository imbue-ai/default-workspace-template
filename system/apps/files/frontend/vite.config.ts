import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { configDefaults } from "vitest/config";
import path from "path";
import { themeBoot } from "@imbue/workspace-ui/src/themes/themeBoot";

export default defineConfig({
  test: {
    exclude: [...configDefaults.exclude, "dist/**"],
  },
  // themeBoot wears the theme this origin last wore before the first paint (workspace-themes plan section 5.3).
  plugins: [tailwindcss(), themeBoot()],
  root: ".",
  resolve: {
    alias: {
      // The Imbue Studio embed contract -- the single sanctioned postMessage channel
      // between this UI and the embedding Imbue Studio chrome -- is fetched from the
      // mngr commit pyproject.toml pins (system/scripts/fetch_mngr_assets.sh, run
      // by the npm workspace root's prebuild) so both sides always ship from one source.
      // Types come from the library's src/embed-contract.d.ts; keep the two in sync.
      "@minds/embed-contract": path.resolve(
        __dirname,
        "../../../vendor/mngr-assets/apps/minds/imbue/minds/desktop_client/static/embed_contract.js",
      ),
    },
  },
  // Where the Files server (system/apps/files/src/files_app/server.py) serves the build. Its files are named by their
  // content, so the server lets a browser keep them; index.html, which names this build's, it serves afresh.
  base: "/__files/assets/",
  build: {
    outDir: path.resolve(__dirname, "../static"),
    emptyOutDir: true,
    assetsDir: "",
  },
});
