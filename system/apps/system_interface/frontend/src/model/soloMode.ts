/**
 * Solo mode (the pull-out-window spec, section 7.5): ``/?solo=<window-id>`` asks the shell to show that one
 * window edge to edge and nothing else, which is what a pulled-out window's desktop window loads. Read once at
 * boot and stripped from the URL like the deep-link parameters; this module only reads and strips.
 */

import { withoutParams } from "./queryParams";

const SOLO_PARAM = "solo";

/** The window a query string asks the shell to show alone, or null when it asks for the whole desktop. */
export function parseSoloWindowId(search: string): string | null {
  const value = new URLSearchParams(search).get(SOLO_PARAM);
  return value === null || value === "" ? null : value;
}

/** The query string with the solo parameter removed (other parameters kept), "" when none remain. */
export function stripSoloParam(search: string): string {
  return withoutParams(search, [SOLO_PARAM]);
}
