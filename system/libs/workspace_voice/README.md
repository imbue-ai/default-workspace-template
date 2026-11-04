# workspace_voice

Dictation for the workspace's pages (the workspace-voice-input spec in the
mngr repository): speech recognized on the user's own device, in a worker the
page starts, so a workspace pays no memory for voice. Source only, like
`workspace_ui`: there is no build here, each app's vite build compiles the
modules it imports (`@imbue/workspace-voice/src/<module>`), and the package is a
member of the npm workspace rooted at `system/package.json`. The chat's composer
is the one surface that uses it.

- `src/engines.ts` and `src/voice_models.json`: the engine registry. Every
  engine dictation can run, with the Hugging Face commit its files are taken
  at, each file's size and SHA-256, a variant per device (WebGPU or
  WebAssembly), the most audio one call may carry, and its licence. Moonshine
  Base is the default and, so far, the only engine.
- `src/download.ts`: the download of an engine's pinned files, straight from
  Hugging Face, each checked against its size and hash before the engine sees
  it, and kept only in memory.
- `src/workerProtocol.ts`, `src/workerHost.ts`, `src/workerClient.ts`,
  `src/engineWorker.ts`: the engine worker. One SharedWorker per workspace
  origin and engine (a dedicated worker where the browser has none), which
  every chat page connects to; it downloads the files once, picks the device,
  loads the engine, transcribes one job at a time in arrival order, and
  releases the engine after two minutes idle.
- `src/transformersEngine.ts`: the transformers.js adapter the worker loads an
  engine with, its caches off; imported by the worker alone, since it carries
  the speech runtime.
- `src/speechDetector.ts`: Silero VAD v5 through vad-web, with Sculptor's
  tuning, scoring every 32 ms frame and marking utterances.
- `src/streamingTurn.ts`: one utterance's streaming state, ported from
  Sculptor's voice entry: a bounded window re-transcribed for the live tail,
  sliced at real pauses into heads committed once each, so dictation has no
  length limit and each preview costs the same.
- `src/scheduler.ts`: the one inference lane: commits in audio order ahead of
  the newest preview, a failed commit retried twice in place, and the words
  held for Retry or Discard when it keeps failing.
- `src/dictation.ts`: what a composer drives: start, stop, cancel, and the
  events it draws from (the state, the download's progress, the input level,
  final words, the unfinished tail, and problems). One dictation runs per
  workspace: a start anywhere stops the one running elsewhere.
- `src/liveSpan.ts`: where the words land in a box of text: at the caret, the
  unfinished tail rewritten in place, only final words saved, and words fitted
  to the sentence around them.
- `src/microphone.ts` and `src/support.ts`: the microphone's failures in the
  user's words, whether a page can offer dictation at all, and the shortcut as
  the platform spells it.
- `src/runtimeAssets.ts`: the vite plugin that ships the runtime's own
  WebAssembly, the detector's worklet, and the detector's model with an app's
  build under `assets/voice/`, so no code loads from a third-party host.

```bash
cd system && npm ci                                    # every frontend's dependencies
cd system/libs/workspace_voice && npx vitest run       # this package's tests
cd system && npm run lint && npm run format:check && npm run typecheck --workspace=libs/workspace_voice
```
