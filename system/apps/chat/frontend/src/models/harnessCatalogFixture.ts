/** A harness catalog for tests, carrying the names the backend's table gives the harnesses. */

import type { HarnessCatalog } from "./HarnessCatalog";

const LABEL_BY_HARNESS: Record<string, string> = {
  claude: "Claude Code",
  codex: "Codex",
  "pi-coding": "Pi",
  opencode: "OpenCode",
  antigravity: "Antigravity CLI",
};

const COMPACTABLE_HARNESSES = new Set(["claude", "codex", "pi-coding"]);

/** What `getHarnessCatalog` answers in a test: a catalog with the harness's name, switch mode and
 *  whether it can be compacted, and no models. */
export function harnessCatalogFixture(harness: string | undefined): HarnessCatalog | null {
  const label = harness === undefined ? undefined : LABEL_BY_HARNESS[harness];
  if (label === undefined) return null;
  return {
    label,
    options: [],
    switch_mode: harness === "antigravity" ? "read_only" : "eager_then_reconcile",
    picker_mode: "list",
    native_atomic_shoulder_tap_possible: false,
    supports_compaction: COMPACTABLE_HARNESSES.has(harness ?? ""),
    can_interrupt_compaction: harness === "claude",
    popups: [],
  };
}
