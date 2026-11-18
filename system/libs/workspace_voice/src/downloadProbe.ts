/**
 * Whether the speech model is already on this device: asked by a page as it mounts its composer, so the mic can
 * offer the one-time download as its own step rather than springing it on the first press. The page picks the
 * device the worker would (``devicePick.ts``), takes the engine's variant for it, and asks its own Cache Storage
 * whether every pinned file of that variant is kept. Presence is enough here: the load checks every file's size
 * and hash again before the engine sees it, so a kept file that fails its check costs one download, not a wrong
 * answer to the user.
 */

import type { GpuNavigatorLike } from "./devicePick";
import { pickDevice } from "./devicePick";
import type { Device } from "./engines";
import { engineSpec, engineVariant, pinnedFileUrl, variantBytes, variantFiles } from "./engines";
import type { PinnedFileStore } from "./fileStore";
import { defaultPinnedFileStore } from "./fileStore";
import type { PageDictationOptions } from "./support";

export interface EngineDownloadProbe {
  /** Whether every pinned file of the variant this device runs is kept here. */
  readonly isDownloaded: boolean;
  /** The bytes that variant is, which the first load downloads when it is not kept. */
  readonly totalBytes: number;
  readonly device: Device;
}

export interface DownloadProbeDeps {
  store: PinnedFileStore;
  navigator: GpuNavigatorLike;
}

export function defaultDownloadProbeDeps(): DownloadProbeDeps {
  return { store: defaultPinnedFileStore(), navigator: navigator as Navigator & GpuNavigatorLike };
}

export async function probeEngineDownload(
  options: PageDictationOptions,
  deps: DownloadProbeDeps = defaultDownloadProbeDeps(),
): Promise<EngineDownloadProbe> {
  const spec = engineSpec(options.engineId);
  const device = await pickDevice(spec, options.preferredDevice, deps.navigator);
  const variant = engineVariant(spec, device);
  if (variant === null) {
    throw new Error(`${spec.displayName} has no ${device} variant; the device pick must offer one it has`);
  }
  const kept = await Promise.all(variantFiles(spec, variant).map((file) => deps.store.has(pinnedFileUrl(spec, file))));
  return { isDownloaded: kept.every((isKept) => isKept), totalBytes: variantBytes(spec, variant), device };
}
