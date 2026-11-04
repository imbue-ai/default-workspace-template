/**
 * The transformers.js runtime adapter: loads an engine from the bytes the worker downloaded and checked, with the
 * library's own caches off, so the page keeps no copy of a model file (the workspace-voice-input spec, D6), and
 * runs it on the device the worker picked. Imported by the worker alone: it carries the speech runtime.
 */

import { env, pipeline } from "@huggingface/transformers";
import type { EngineSpec, EngineVariant } from "./engines";
import { maxTokensFor } from "./engines";
import type { SpeechEngine } from "./speechEngine";
import { SAMPLE_RATE } from "./speechEngine";

/** The host the runtime is told its models live at. It does not exist: every file the runtime asks for is
 *  answered from the checked bytes, and a file nobody pinned is a 404 rather than a trip to the internet. */
export const PINNED_FILE_HOST = "https://workspace-voice.invalid/";

type FetchLike = typeof globalThis.fetch;

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

/**
 * Answer the runtime's requests for ``repo``'s files from ``files`` by patching the realm's ``fetch``: the worker's
 * realm is the engine's own, and the runtime fetches with a bare ``fetch`` it offers no hook for. Every other
 * request passes through. Answers the function that restores ``fetch``.
 */
export function installPinnedFileServer(
  repo: string,
  files: ReadonlyMap<string, ArrayBuffer>,
  realm: { fetch: FetchLike } = globalThis,
): () => void {
  const passThrough = realm.fetch;
  const prefix = `${PINNED_FILE_HOST}${repo}/resolve/`;
  const patched: FetchLike = (input, init) => {
    const url = requestUrl(input);
    if (!url.startsWith(PINNED_FILE_HOST)) return passThrough.call(realm, input, init);
    // ``<host><repo>/resolve/<revision>/<path>``: the revision is the runtime's, the path is ours.
    const path = url.startsWith(prefix) ? url.slice(prefix.length).replace(/^[^/]*\//, "") : "";
    const bytes = files.get(path);
    if (bytes === undefined) return Promise.resolve(new Response(null, { status: 404 }));
    return Promise.resolve(new Response(bytes.slice(0), { status: 200 }));
  };
  realm.fetch = patched;
  return () => {
    if (realm.fetch === patched) realm.fetch = passThrough;
  };
}

export interface TransformersLoadOptions {
  /** Where the page serves the runtime's own code: its WebAssembly under ``transformers-ort/``. */
  assetBase: string;
}

/** Load ``variant`` of ``spec`` from its checked bytes. */
export async function loadTransformersEngine(
  spec: EngineSpec,
  variant: EngineVariant,
  files: Map<string, ArrayBuffer>,
  options: TransformersLoadOptions,
): Promise<SpeechEngine> {
  env.allowLocalModels = false;
  env.allowRemoteModels = true;
  env.useBrowserCache = false;
  env.useCustomCache = false;
  env.remoteHost = PINNED_FILE_HOST;
  env.remotePathTemplate = "{model}/resolve/{revision}/";
  const wasm = env.backends.onnx.wasm;
  if (wasm !== undefined) {
    wasm.wasmPaths = `${options.assetBase}transformers-ort/`;
    // Nothing isolates the page cross-origin, so there is no SharedArrayBuffer for a second thread.
    wasm.numThreads = 1;
    wasm.proxy = false;
  }
  const restoreFetch = installPinnedFileServer(spec.repo, files);
  let transcriber: Awaited<ReturnType<typeof pipeline<"automatic-speech-recognition">>>;
  try {
    transcriber = await pipeline<"automatic-speech-recognition">("automatic-speech-recognition", spec.repo, {
      dtype: variant.dtype,
      device: variant.device,
    });
  } finally {
    restoreFetch();
  }
  return {
    async transcribe(samples16k) {
      const maxNewTokens = maxTokensFor(spec, samples16k.length / SAMPLE_RATE);
      const output = await transcriber(samples16k, maxNewTokens === null ? {} : { max_new_tokens: maxNewTokens });
      const first = Array.isArray(output) ? output[0] : output;
      return { text: (first?.text ?? "").trim(), words: null };
    },
    async dispose() {
      await transcriber.dispose();
    },
  };
}
