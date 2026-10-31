import path from "node:path";
import { defineConfig } from "vitest/config";

// The library has no build of its own (each app's vite build compiles its source); this
// configures only its tests, aliasing the vendored embed contract as each app's vite config does.
export default defineConfig({
  resolve: {
    alias: {
      "@minds/embed-contract": path.resolve(
        __dirname,
        "../../vendor/mngr-assets/apps/minds/imbue/minds/desktop_client/static/embed_contract.js",
      ),
    },
  },
  test: {},
});
