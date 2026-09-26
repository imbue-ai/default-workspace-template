/**
 * Desktop states for the reducer and view tests, built the way the store builds them: the reducers run over the
 * records the shell would send.
 */

import type { AppRecord } from "../model/records";
import { initialDesktopState, reduceDesktopState } from "../reducers/desktopState";
import type { DesktopState } from "../reducers/desktopState";
import { desktopRecord } from "./records";

const MODES = { isCompact: false, isTouch: false };

/** A client on a machine offering ``apps``, with one active desktop ``home`` holding no windows. */
export function desktopStateWithApps(apps: readonly AppRecord[]): DesktopState {
  let next = initialDesktopState("client-1", MODES);
  next = reduceDesktopState(next, { type: "apps_updated", apps: [...apps] });
  next = reduceDesktopState(next, { type: "desktops_updated", desktops: [desktopRecord("home")] });
  return reduceDesktopState(next, { type: "desktop_activated", desktopId: "home" });
}
