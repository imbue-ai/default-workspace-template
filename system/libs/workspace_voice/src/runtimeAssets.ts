/**
 * The vite plugin that ships the voice runtime's own code and model with an app's build, so no code loads from
 * a third-party host (the workspace-voice-input spec, D6): the ONNX Runtime WebAssembly the speech runtime loads,
 * the one the detector loads (each library embeds its own build, so each gets the set it expects), the detector's
 * audio worklet, and the detector's own model, which ships with its library. In a dev server the files stream
 * straight from ``node_modules``; a build emits them at the stable path the page's URLs name.
 */

import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import type { Plugin } from "vite";

/** Where the files sit under an app's served assets; the page resolves it under its base path. */
export const VOICE_ASSETS_PATH = "assets/voice/";

interface RuntimeAsset {
  /** The path under ``VOICE_ASSETS_PATH``. */
  served: string;
  source: string;
}

const CONTENT_TYPES: Record<string, string> = {
  ".wasm": "application/wasm",
  ".mjs": "text/javascript",
  ".js": "text/javascript",
  ".onnx": "application/octet-stream",
};

function runtimeAssets(): RuntimeAsset[] {
  const require = createRequire(import.meta.url);
  const transformersDist = path.join(path.dirname(require.resolve("@huggingface/transformers/package.json")), "dist");
  const vadWebPackage = require.resolve("@ricky0123/vad-web/package.json");
  const vadWebDist = path.join(path.dirname(vadWebPackage), "dist");
  // The detector's own ONNX Runtime, wherever the package manager put it relative to the detector.
  const vadOrtDist = path.join(
    path.dirname(createRequire(vadWebPackage).resolve("onnxruntime-web/package.json")),
    "dist",
  );
  return [
    {
      served: "transformers-ort/ort-wasm-simd-threaded.jsep.mjs",
      source: path.join(transformersDist, "ort-wasm-simd-threaded.jsep.mjs"),
    },
    {
      served: "transformers-ort/ort-wasm-simd-threaded.jsep.wasm",
      source: path.join(transformersDist, "ort-wasm-simd-threaded.jsep.wasm"),
    },
    { served: "vad-ort/ort-wasm-simd-threaded.mjs", source: path.join(vadOrtDist, "ort-wasm-simd-threaded.mjs") },
    { served: "vad-ort/ort-wasm-simd-threaded.wasm", source: path.join(vadOrtDist, "ort-wasm-simd-threaded.wasm") },
    { served: "vad/vad.worklet.bundle.min.js", source: path.join(vadWebDist, "vad.worklet.bundle.min.js") },
    { served: "vad/silero_vad_v5.onnx", source: path.join(vadWebDist, "silero_vad_v5.onnx") },
  ];
}

export function voiceRuntimeAssets(): Plugin {
  const assets = runtimeAssets();
  const prefix = `/${VOICE_ASSETS_PATH}`;
  return {
    name: "workspace-voice:runtime-assets",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const url = (request.url ?? "").split("?")[0];
        const at = url.indexOf(prefix);
        if (at === -1) {
          next();
          return;
        }
        const served = url.slice(at + prefix.length);
        const asset = assets.find((candidate) => candidate.served === served);
        if (asset === undefined) {
          next();
          return;
        }
        response.setHeader("Content-Type", CONTENT_TYPES[path.extname(asset.source)] ?? "application/octet-stream");
        response.setHeader("Cache-Control", "no-cache");
        fs.createReadStream(asset.source).pipe(response);
      });
    },
    generateBundle() {
      for (const asset of assets) {
        this.emitFile({
          type: "asset",
          fileName: `${VOICE_ASSETS_PATH}${asset.served}`,
          source: fs.readFileSync(asset.source),
        });
      }
    },
  };
}
