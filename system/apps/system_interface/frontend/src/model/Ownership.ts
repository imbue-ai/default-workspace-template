/**
 * Whose desktop a desktop is (the multiplayer spec, section 8.1): a convention the overview, the person cards,
 * the launcher's window rows, and the viewing chrome read, never a lock. A desktop with no owner is the workspace
 * owner's, under either of the owner's keys; any other is its owner's alone. One whose owner is no longer in the
 * roster is left behind: it lists in the owner's overview, so the owner can view and delete it.
 */

import type { OwnIdentity } from "./Presence";
import type { Desktop, RosterParticipant } from "./records";

/** Whether the desktop is the viewer's own. Before a heartbeat has answered who the viewer is (``identity`` null)
 *  the owner's desktops read as the viewer's: a workspace nobody has shared has nobody else to be. */
export function isOwnDesktop(desktop: Desktop, identity: OwnIdentity | null): boolean {
  if (desktop.owner === null) return identity === null || identity.owner;
  return identity !== null && desktop.owner === identity.participant;
}

/** The viewer's own desktops, in the shell's order. */
export function ownDesktops(desktops: readonly Desktop[], identity: OwnIdentity | null): Desktop[] {
  return desktops.filter((desktop) => isOwnDesktop(desktop, identity));
}

/** The desktops that are a roster entry's: the ones carrying their key, and, for the owner, the ones with none. */
export function desktopsOf(entry: RosterParticipant, desktops: readonly Desktop[]): Desktop[] {
  return desktops.filter((desktop) => desktop.owner === entry.key || (entry.is_owner && desktop.owner === null));
}

/** The roster entry of a desktop's owner: the owner's entry for a desktop with no key, else the entry carrying
 *  the key; null when nobody in the roster owns it (left behind). */
export function ownerEntryOf(desktop: Desktop, roster: readonly RosterParticipant[]): RosterParticipant | null {
  if (desktop.owner === null) return roster.find((entry) => entry.is_owner) ?? null;
  const key = desktop.owner;
  return roster.find((entry) => entry.key === key) ?? null;
}

/** Whether the desktop's owner has left the roster: nobody's card lists it, and the owner's overview lists it
 *  under "Left behind". A desktop with no key is never left behind. */
export function isLeftBehind(desktop: Desktop, roster: readonly RosterParticipant[]): boolean {
  return desktop.owner !== null && ownerEntryOf(desktop, roster) === null;
}

/** The desktops left behind, which the owner alone sees. */
export function leftBehindDesktops(
  desktops: readonly Desktop[],
  roster: readonly RosterParticipant[],
  identity: OwnIdentity | null,
): Desktop[] {
  if (identity !== null && !identity.owner) return [];
  return desktops.filter((desktop) => isLeftBehind(desktop, roster));
}
