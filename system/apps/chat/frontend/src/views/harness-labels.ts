/** The user-facing name of a harness, read off its catalog (`HarnessCatalog.label`). */

import { getHarnessCatalog } from "../models/HarnessCatalog";

// Before the catalogs have loaded, or for a harness the backend does not list, the raw name
// shows rather than nothing. A view that reads it while rendering picks the label up on the load's
// redraw; text captured once (a failure notice's title) keeps the raw name.
export function harnessLabel(harness: string): string {
  return getHarnessCatalog(harness)?.label ?? harness;
}
