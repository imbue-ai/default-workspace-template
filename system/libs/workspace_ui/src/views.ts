/**
 * The one view that is not a project: Everything, the unfiltered view of the whole machine. Its
 * id reaches an app page in the shell's handshake, so both sides spell it here.
 */

/** The id of the reserved unfiltered view, matching the backend's ``EVERYTHING_VIEW_ID``. */
export const EVERYTHING_VIEW_ID = "everything";

/** The display name of the unfiltered view, matching the backend's ``EVERYTHING_VIEW_NAME``. */
export const EVERYTHING_VIEW_NAME = "Everything";

/** Whether a view id addresses the unfiltered view rather than a project. */
export function isEverythingView(viewId: string): boolean {
  return viewId === EVERYTHING_VIEW_ID;
}
