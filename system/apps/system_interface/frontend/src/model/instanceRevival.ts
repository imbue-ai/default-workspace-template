/** Where starting a stopped preview or UI sketch again stands: not asked, under way, or failed with the shell's
 *  reason; ``canRetry`` is false when no start can bring it back, only a new one. */
export type InstanceRevival =
  | { readonly kind: "idle" }
  | { readonly kind: "starting" }
  | { readonly kind: "failed"; readonly reason: string; readonly canRetry: boolean };

export const IDLE_REVIVAL: InstanceRevival = { kind: "idle" };
