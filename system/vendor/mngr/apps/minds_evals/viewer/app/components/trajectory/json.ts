/**
 * Narrowing for the JSON the viewer reads without a schema: ATIF `extra` blocks, which producers
 * fill with whatever they like, and the eval's own manifests. The typed `Trajectory` and `Step`
 * models stop at `unknown` there, so every read past that point starts here.
 */

/** `value` as an object, or null when it is anything else -- an array and null included. */
export function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}
