/** What the page's own URL asks of the viewer (docs/system/specs/screen/spec.md section 11). ``strip=off`` (an auth
 *  sandbox's frame, where the hosted chrome draws its own controls) hides the control row: the screencast alert and
 *  notices show as a small banner over the stage while they apply, and a press or key on the stage while another
 *  session controls takes control. Any other value, or none, keeps the strip. The gate's owner-token redirect keeps
 *  the rest of the query, so the parameter survives it. */

import type { ControlDocument } from "./state";
import { isUnclaimedHandoff } from "./state";

export const STRIP_PARAMETER = "strip";
export const STRIP_OFF = "off";

export interface ViewerOptions {
  /** Whether the control row is shown; without it the alert and notices are a banner over the stage. */
  readonly isStripShown: boolean;
}

export function parseViewerOptions(search: string): ViewerOptions {
  return { isStripShown: new URLSearchParams(search).get(STRIP_PARAMETER) !== STRIP_OFF };
}

/** Whether another human session holds control, which a stripless viewer takes at the user's first press or key:
 *  not this session, not an agent (its dim overlay offers Take control), and not a handoff any input claims. */
export function isHeldByAnotherSession(control: ControlDocument, ownSessionId: string | null): boolean {
  return (
    control.controller === "human" &&
    ownSessionId !== null &&
    !isUnclaimedHandoff(control) &&
    control.human_session_id !== ownSessionId
  );
}
