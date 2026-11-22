import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { configDefaults } from "vitest/config";
import path from "path";
import { themeBoot } from "@imbue/workspace-ui/src/themes/themeBoot";
// By path rather than by package name: vite loads a config's bare imports through Node as they are, which
// cannot follow the plugin's extensionless imports, while a relative import is bundled with the config.
import { voiceRuntimeAssets } from "../../../libs/workspace_voice/src/runtimeAssets";

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
  // themeBoot wears the theme this origin last wore before the first paint (workspace-themes plan section 5.3);
  // voiceRuntimeAssets ships the dictation's WebAssembly, worklet, and detector model under assets/voice/.
  plugins: [tailwindcss(), themeBoot(), voiceRuntimeAssets()],
  // The dictation's engine worker is a module worker that loads the speech runtime on its own; the default
  // iife format cannot split it.
  worker: { format: "es" },
  publicDir: "media",
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
    outDir: path.resolve(__dirname, "../imbue/chat/static"),
    emptyOutDir: true,
    rollupOptions: {
      // The chat document, which the chat app serves at /<agent-id>, and the chat root (the
      // chat list beside an inner chat frame), served at / and /new.
      input: {
        chat: path.resolve(__dirname, "chat.html"),
        root: path.resolve(__dirname, "root.html"),
      },
    },
  },
  server: {
    proxy: {
      "/api": { target: "http://localhost:8010", ws: true },
    },
  },
});
