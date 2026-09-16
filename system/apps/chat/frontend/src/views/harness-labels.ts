/** The user-facing name of a harness, read off its catalog (`HarnessCatalog.label`). */

import { getHarnessCatalog } from "../models/HarnessCatalog";

// Before the catalogs have loaded, or for a harness the backend does not list, the raw name
// shows rather than nothing. A view reading it on render picks the label up on the load's redraw;
// text built from it earlier keeps the raw name.
export function harnessLabel(harness: string): string {
  return getHarnessCatalog(harness)?.label ?? harness;
}
