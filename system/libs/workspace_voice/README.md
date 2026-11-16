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
  WebAssembly), the most audio one call may carry, and its licence; and the
  engine each kind of device runs by default: Moonshine Base on a desktop, and
  Moonshine Tiny, the lighter model and the smaller download, on a touch
  device (a phone or tablet, told by a finger being the primary pointer),
  which also runs it on WebAssembly's quantized weights even where it has
  WebGPU, a third of the memory of the full-precision ones. A browser's
  overrides name any registered engine and device instead, for a side-by-side
  evaluation.
- `src/download.ts` and `src/fileStore.ts`: getting an engine's pinned files:
  from the store the last load kept them in when they check out, else straight
  from Hugging Face, each checked against its size and hash before the engine
  sees it and kept for next time. The store is the browser's own Cache Storage
  on the workspace's origin, one entry per pinned URL, pruned to the variant in
  use, evicted by the browser under storage pressure like any site's, and never
  trusted: every file read back is checked again.
- `src/workerProtocol.ts`, `src/workerHost.ts`, `src/workerClient.ts`,
  `src/engineWorker.ts`: the engine worker. One SharedWorker per workspace
  origin and engine (a dedicated worker where the browser has none), which
  every chat page connects to; it gets the files, picks the device, loads the
  engine, transcribes one job at a time in arrival order, and keeps the engine
  loaded for as long as a page of the workspace is open. It tells each page
  what it already holds as the page connects, and every page each step of a
  load, whichever page asked for it.
- `src/devicePick.ts`: the device an engine runs on here (WebGPU with an
  adapter, else WebAssembly, the page's preference first), picked the same way
  by the worker for the load and by the page for the probe below.
- `src/downloadProbe.ts`: whether the speech model is already on this device,
  asked by a page as its composer mounts: the files of the variant this device
  runs, looked for in the page's own Cache Storage, with the size the download
  would be. Presence alone; the load checks every file again.
- `src/speechModel.ts`: the page's speech models, one managed load per engine
  held in a TanStack Query cache (`@tanstack/query-core`). A surface observes
  an engine's load, asks for it (one load runs however many ask, and a load in
  flight is joined), reads the download's progress, finds out what the
  workspace's worker already holds, and gets the page's port to transcribe
  on, all from one place. The worker is the authority: what it tells the
  page's port is mirrored into the cache, so a page learns a model came ready
  whichever page asked, and a page that hears another ask for a model connects
  to hear the worker load it.
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
- `src/dictation.ts`: what a composer drives once the speech model is loaded:
  start, stop, cancel, and the events it draws from (the state, the input
  level, final words, the unfinished tail, and problems). A dictation loads
  nothing itself: the composer asks the page's speech models for the model
  first, and the dictation transcribes on the page's port to the engine
  worker, which it leaves open when it is disposed.
  `describeSpeechModelLoadError` puts a failed load in the words a composer
  shows. One dictation runs per workspace: a start anywhere stops the one
  running elsewhere.
- `src/readyChime.ts`: the two short bell tones a composer plays as dictation
  reaches listening, so a user who pressed the mic and looked away knows when
  to speak; woken from the press, since the browser lets a page sound only
  after a gesture.
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
