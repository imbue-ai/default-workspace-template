/** The user-facing name of a harness, read off its catalog (`HarnessCatalog.label`). */

import { getHarnessCatalog } from "../models/HarnessCatalog";

// Before the catalogs have loaded, or for a harness the backend does not list, the raw name
// shows rather than nothing; the load's redraw replaces it.
export function harnessLabel(harness: string): string {
  return getHarnessCatalog(harness)?.label ?? harness;
}
