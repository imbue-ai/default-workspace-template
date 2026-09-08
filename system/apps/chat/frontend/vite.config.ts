import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { configDefaults } from "vitest/config";
import path from "path";

export default defineConfig({
  // `dist/` is not part of this project's output -- the bundle goes to
  // `build.outDir` below, and nothing reads `dist/`. Were a stray build ever
  // to emit one, vitest's default include would collect the compiled COPY of
  // every test beside its source: the same suite twice, with one half frozen
  // at whenever that build ran. Excluded so a stale directory cannot quietly
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
      // between this UI and the embedding minds chrome -- is consumed from the
      // vendored mngr tree so both sides always ship from one source of truth.
      // Types come from the library's src/embed-contract.d.ts; keep the two in sync.
      "@minds/embed-contract": path.resolve(
        __dirname,
        "../../../vendor/mngr/apps/minds/imbue/minds/desktop_client/static/embed_contract.js",
      ),
    },
  },
  build: {
    outDir: path.resolve(__dirname, "../imbue/chat/static"),
    emptyOutDir: true,
    rollupOptions: {
      // The chat document, which the chat app serves at /<agent-id>.
      input: {
        chat: path.resolve(__dirname, "chat.html"),
      },
    },
  },
  server: {
    proxy: {
      "/api": { target: "http://localhost:8010", ws: true },
      "/_instances": { target: "http://localhost:8010" },
    },
  },
});
