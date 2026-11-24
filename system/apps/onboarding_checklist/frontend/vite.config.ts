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
  build: {
    // Inside the Python package, where the app serves it from.
    outDir: path.resolve(__dirname, "../src/onboarding_checklist/static"),
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": { target: "http://localhost:8070" },
    },
  },
});
