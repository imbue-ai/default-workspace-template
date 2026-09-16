/** A harness catalog for tests, carrying the names the backend's table gives the harnesses. */

import type { HarnessCatalog } from "./HarnessCatalog";

const LABEL_BY_HARNESS: Record<string, string> = {
  claude: "Claude Code",
  codex: "Codex",
  "pi-coding": "Pi",
  opencode: "OpenCode",
  antigravity: "Antigravity CLI",
};

/** What `getHarnessCatalog` answers in a test: a catalog with the harness's name and no models. */
export function harnessCatalogFixture(harness: string | undefined): HarnessCatalog | null {
  const label = harness === undefined ? undefined : LABEL_BY_HARNESS[harness];
  if (label === undefined) return null;
  return {
    label,
    options: [],
    switch_mode: "eager_then_reconcile",
    picker_mode: "list",
    native_atomic_shoulder_tap_possible: false,
    popups: [],
  };
}
