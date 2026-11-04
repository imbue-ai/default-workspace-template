/**
 * The engine registry: every speech engine dictation can run, with the files each needs and where they come from
 * (the workspace-voice-input spec, D2 and D6). The engines are data, ``voice_models.json``, read and checked here
 * once at load; the only code an engine needs is its runtime adapter, which the worker alone imports.
 *
 * Every file is named by a Hugging Face commit, so the same bytes come down every time, and carries the size and
 * SHA-256 the download is checked against (``download.ts``).
 */

import registry from "./voice_models.json";

export type EngineId = string;

/** Where the engine runs: WebGPU when the device has an adapter, else single-threaded WebAssembly. */
export type Device = "webgpu" | "wasm";

/** The kind of device an engine may be offered on (the spec's D9: a phone is offered only what fits it). */
export type DeviceClass = "desktop" | "touch";

/** The runtime an engine's adapter is written against. */
export type EngineRuntime = "transformers-js";

/** The weights' precision, in the runtime's vocabulary. */
export type Dtype = "fp32" | "fp16" | "q8" | "int8" | "uint8" | "q4" | "q4f16" | "bnb4";

/** One file of an engine, downloaded from Hugging Face at the engine's commit and checked against its hash. */
export interface PinnedFile {
  /** The path inside the model repository, which is also the path the runtime asks for. */
  readonly path: string;
  readonly sha256: string;
  readonly bytes: number;
}

export interface EngineVariant {
  readonly device: Device;
  readonly dtype: Dtype;
  /** The files only this variant needs, beside the engine's shared files. */
  readonly files: readonly PinnedFile[];
}

export interface EngineLicence {
  readonly name: string;
  readonly attribution: string | null;
}

export interface EngineSpec {
  readonly id: EngineId;
  readonly displayName: string;
  readonly runtime: EngineRuntime;
  /** The Hugging Face repository the files come from. */
  readonly repo: string;
  /** The commit every file is taken at. */
  readonly commit: string;
  /** The files every variant needs (the configuration and the tokenizer). */
  readonly sharedFiles: readonly PinnedFile[];
  readonly variants: readonly EngineVariant[];
  /** The most audio one call may carry; sizes the streaming window. */
  readonly maxSecondsPerCall: number;
  readonly emitsWordTimestamps: boolean;
  /** The decoder's output cap per second of audio, for a sequence-to-sequence model that can run away; null for
   *  a model that cannot. */
  readonly maxTokensPerSecond: number | null;
  readonly licence: EngineLicence;
  readonly devices: readonly DeviceClass[];
}

export const DEFAULT_ENGINE_ID: EngineId = "moonshine-base";

const SHA256_HEX = /^[0-9a-f]{64}$/;
const COMMIT_HEX = /^[0-9a-f]{40}$/;
const DEVICES: readonly Device[] = ["webgpu", "wasm"];
const DTYPES: readonly Dtype[] = ["fp32", "fp16", "q8", "int8", "uint8", "q4", "q4f16", "bnb4"];
const DEVICE_CLASSES: readonly DeviceClass[] = ["desktop", "touch"];
const RUNTIMES: readonly EngineRuntime[] = ["transformers-js"];

export class EngineRegistryError extends Error {}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asString(value: unknown, what: string): string {
  if (typeof value !== "string" || value === "") throw new EngineRegistryError(`${what} must be a non-empty string`);
  return value;
}

function asPositiveNumber(value: unknown, what: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    throw new EngineRegistryError(`${what} must be a positive number`);
  }
  return value;
}

function asOneOf<T extends string>(value: unknown, allowed: readonly T[], what: string): T {
  const found = allowed.find((candidate) => candidate === value);
  if (found === undefined) throw new EngineRegistryError(`${what} must be one of ${allowed.join(", ")}`);
  return found;
}

function parsePinnedFile(raw: unknown, what: string): PinnedFile {
  if (!isRecord(raw)) throw new EngineRegistryError(`${what} must be a table`);
  const sha256 = asString(raw.sha256, `${what}.sha256`);
  if (!SHA256_HEX.test(sha256)) throw new EngineRegistryError(`${what}.sha256 must be 64 lowercase hex digits`);
  const bytes = asPositiveNumber(raw.bytes, `${what}.bytes`);
  if (!Number.isInteger(bytes)) throw new EngineRegistryError(`${what}.bytes must be an integer`);
  return { path: asString(raw.path, `${what}.path`), sha256, bytes };
}

function parseFiles(raw: unknown, what: string): PinnedFile[] {
  if (!Array.isArray(raw)) throw new EngineRegistryError(`${what} must be an array`);
  return raw.map((file, index) => parsePinnedFile(file, `${what}[${index}]`));
}

function parseEngine(raw: unknown, what: string): EngineSpec {
  if (!isRecord(raw)) throw new EngineRegistryError(`${what} must be a table`);
  const id = asString(raw.id, `${what}.id`);
  const commit = asString(raw.commit, `${what}.commit`);
  if (!COMMIT_HEX.test(commit)) throw new EngineRegistryError(`${what}.commit must be a full commit hash`);
  if (!Array.isArray(raw.variants) || raw.variants.length === 0) {
    throw new EngineRegistryError(`${what}.variants must name at least one variant`);
  }
  const sharedFiles = parseFiles(raw.sharedFiles, `${what}.sharedFiles`);
  const variants = raw.variants.map((variant, index): EngineVariant => {
    const variantWhat = `${what}.variants[${index}]`;
    if (!isRecord(variant)) throw new EngineRegistryError(`${variantWhat} must be a table`);
    return {
      device: asOneOf(variant.device, DEVICES, `${variantWhat}.device`),
      dtype: asOneOf(variant.dtype, DTYPES, `${variantWhat}.dtype`),
      files: parseFiles(variant.files, `${variantWhat}.files`),
    };
  });
  const variantDevices = variants.map((variant) => variant.device);
  if (new Set(variantDevices).size !== variantDevices.length) {
    throw new EngineRegistryError(`${what} declares two variants for one device`);
  }
  for (const variant of variants) {
    const paths = [...sharedFiles, ...variant.files].map((file) => file.path);
    if (new Set(paths).size !== paths.length) {
      throw new EngineRegistryError(`${what} names one file twice for ${variant.device}`);
    }
  }
  if (!isRecord(raw.licence)) throw new EngineRegistryError(`${what}.licence must be a table`);
  if (!Array.isArray(raw.devices) || raw.devices.length === 0) {
    throw new EngineRegistryError(`${what}.devices must name at least one kind of device`);
  }
  const maxTokensPerSecond = raw.maxTokensPerSecond;
  return {
    id,
    displayName: asString(raw.displayName, `${what}.displayName`),
    runtime: asOneOf(raw.runtime, RUNTIMES, `${what}.runtime`),
    repo: asString(raw.repo, `${what}.repo`),
    commit,
    sharedFiles,
    variants,
    maxSecondsPerCall: asPositiveNumber(raw.maxSecondsPerCall, `${what}.maxSecondsPerCall`),
    emitsWordTimestamps: raw.emitsWordTimestamps === true,
    maxTokensPerSecond:
      maxTokensPerSecond === null ? null : asPositiveNumber(maxTokensPerSecond, `${what}.maxTokensPerSecond`),
    licence: {
      name: asString(raw.licence.name, `${what}.licence.name`),
      attribution:
        raw.licence.attribution === null ? null : asString(raw.licence.attribution, `${what}.licence.attribution`),
    },
    devices: raw.devices.map((device, index) => asOneOf(device, DEVICE_CLASSES, `${what}.devices[${index}]`)),
  };
}

/** The registry as ``voice_models.json`` declares it, checked once; a malformed registry fails the module load. */
export function parseEngineRegistry(raw: unknown): readonly EngineSpec[] {
  if (!isRecord(raw) || !Array.isArray(raw.engines)) throw new EngineRegistryError("the registry must list engines");
  const engines = raw.engines.map((engine, index) => parseEngine(engine, `engines[${index}]`));
  const ids = engines.map((engine) => engine.id);
  if (new Set(ids).size !== ids.length) throw new EngineRegistryError("two engines share an id");
  return engines;
}

export const ENGINES: readonly EngineSpec[] = parseEngineRegistry(registry);

/** The engine named ``id``; a name the registry does not know is a programming error. */
export function engineSpec(id: EngineId): EngineSpec {
  const spec = ENGINES.find((candidate) => candidate.id === id);
  if (spec === undefined) throw new EngineRegistryError(`no engine is registered as ${JSON.stringify(id)}`);
  return spec;
}

/** The engine's variant for ``device``, or null when it has none for it. */
export function engineVariant(spec: EngineSpec, device: Device): EngineVariant | null {
  return spec.variants.find((variant) => variant.device === device) ?? null;
}

/** Every file a variant needs: the engine's shared files, then the variant's own. */
export function variantFiles(spec: EngineSpec, variant: EngineVariant): readonly PinnedFile[] {
  return [...spec.sharedFiles, ...variant.files];
}

/** The bytes a variant downloads. */
export function variantBytes(spec: EngineSpec, variant: EngineVariant): number {
  return variantFiles(spec, variant).reduce((total, file) => total + file.bytes, 0);
}

/** Where every file of the engine, at any commit, is downloaded from. */
export function engineFileUrlPrefix(spec: EngineSpec): string {
  return `https://huggingface.co/${spec.repo}/`;
}

/** The URL a file is downloaded from: the repository at the engine's commit, never a moving branch. */
export function pinnedFileUrl(spec: EngineSpec, file: PinnedFile): string {
  return `${engineFileUrlPrefix(spec)}resolve/${spec.commit}/${file.path}`;
}

/** The window the streaming turn keeps for an engine: cut at a pause past the soft cap, and by force at the hard
 *  cap, which is the most audio one call may carry (the spec's D3). */
export function engineWindowSeconds(spec: EngineSpec): { softCapSeconds: number; hardCapSeconds: number } {
  return { softCapSeconds: Math.max(1, spec.maxSecondsPerCall - 10), hardCapSeconds: spec.maxSecondsPerCall };
}

/** The decoder cap for ``seconds`` of audio on a sequence-to-sequence engine, or null for one that needs none:
 *  ``max(16, ceil(seconds * maxTokensPerSecond) + 10)``, so sub-second audio still gets a sentence's worth of
 *  tokens (the runtime's own ``floor(seconds) * 6`` gives it one). */
export function maxTokensFor(spec: EngineSpec, seconds: number): number | null {
  if (spec.maxTokensPerSecond === null) return null;
  return Math.max(16, Math.ceil(seconds * spec.maxTokensPerSecond) + 10);
}
