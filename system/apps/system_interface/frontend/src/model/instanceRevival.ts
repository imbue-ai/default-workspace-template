/** Where starting a stopped preview or UI sketch again stands: not asked, under way, or failed with the shell's
 *  reason. */
export type InstanceRevival =
  { readonly kind: "idle" } | { readonly kind: "starting" } | { readonly kind: "failed"; readonly reason: string };

export const IDLE_REVIVAL: InstanceRevival = { kind: "idle" };
