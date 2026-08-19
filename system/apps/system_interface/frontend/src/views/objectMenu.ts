/**
 * The verb set for one machine object -- chat, terminal, browser, or app --
 * defined ONCE so the tab's ⋮ menu and the rail's row menu render the
 * identical list off the identical rule, rather than two hand-maintained
 * copies that can (and did) drift: the tab used to offer Refresh / Share /
 * Rename / Hide tab / "Quit <name>" while the rail separately offered
 * "Remove from project" / "Delete from this machine" for the same objects.
 *
 * The four kinds below are the only ones with an object behind them worth
 * naming, sharing or destroying -- a launcher tab is a question about a pane,
 * and a subagent view or an ad-hoc URL page has no persistent identity beyond
 * the panel showing it (see ``memberRefForPanelParams`` in DockviewWorkspace,
 * which now files none of those as members at all). This module has nothing
 * to say about that remainder; the caller keeps whatever minimal menu those
 * still need.
 *
 * What varies **by kind** (which verbs exist, in what order, under what
 * label) is fixed here. What varies **by caller** -- what running a verb
 * actually does -- is not: the tab acts on a live, open panel (Refresh
 * reloads its iframe, Rename opens the tab's own inline editor, Hide tab
 * closes it), while the rail can be showing a *backgrounded* object with no
 * open panel at all, so it needs its own notion of some of the same verbs.
 * ``ObjectMenuActions`` is the seam: every verb's behavior is a callback the
 * caller supplies, and ``objectMenuEntries`` only decides which of those
 * callbacks gets wrapped into a row, in what order, with what icon.
 */

import type { IconName } from "./icons";

/** The four member kinds that carry the consolidated verb set. Deliberately
 *  narrower than ``MemberKind`` in models/Projects (which also has "url" for
 *  an ad-hoc page) -- a caller holding a "url" ref has nothing to build a menu
 *  for in the first place. */
export type ObjectMenuKind = "chat" | "terminal" | "browser" | "app";

/** One actionable row. Carries a label, an icon and a run callback, and
 *  nothing about how it is drawn -- the tab renders these into a floating
 *  card (see ``openTabMenuAt``) and a future rail pass can render the same
 *  list into a context menu or a dropdown without this shape changing. */
export interface ObjectMenuItem {
  label: string;
  iconName: IconName;
  isDestructive?: boolean;
  run: () => void;
}

/** A separator between the reload/share group and the rename/hide/destroy
 *  group -- see ``objectMenuEntries``. */
export const OBJECT_MENU_DIVIDER = "divider";

/** One row of the menu: an actionable item, or a divider. */
export type ObjectMenuEntry = ObjectMenuItem | typeof OBJECT_MENU_DIVIDER;

/**
 * What each verb does for one specific object, supplied by the caller.
 *
 * ``share`` is read only when ``kind`` is "app" (every other kind never
 * offers Share, so a caller building a menu for one of them may simply pass
 * null). ``hideTab`` and ``quit`` are ``null`` to OMIT the verb for this
 * particular object, as opposed to wiring it to a no-op: a backgrounded rail
 * row has no open tab to hide, and a handful of objects -- the workspace's
 * own primary chat, a terminal or browser still allocating its session --
 * have no destroy available yet. ``share`` and ``quit`` each carry their own
 * label because "Share {name}" / "Quit {name}" always names the object the
 * way the surface calling this currently displays it, and only the caller
 * knows what that is right now.
 */
/**
 * Whether a kind's name is the user's to choose.
 *
 * A chat is: it is an mngr agent, whose ref is its stable agent id and whose
 * name is separate metadata, so a rename moves the name everywhere the agent
 * is known -- ``mngr`` included -- and no reference to it has to move.
 *
 * A terminal and a browser are NOT. Neither has an identity apart from its
 * name: a terminal is filed as ``terminal:<tmux session name>`` and a browser
 * as ``service:browser?session=<name>``, where that name is also a live tmux
 * session and a Chromium profile directory on disk. A rename could only ever
 * have been a display name laid over the top, leaving ``tmux ls``, the profile
 * directory, and every stored ref still saying the old one -- so the two are
 * left to their derived numbering instead of being offered a rename that
 * stops at the surface. Giving them a stable id of their own, so the name is
 * free to move like a chat's, is the real fix and is its own piece of work.
 */
export function isRenameableKind(kind: ObjectMenuKind): boolean {
  return kind === "chat" || kind === "app";
}

export interface ObjectMenuActions {
  refresh: () => void;
  share: { label: string; run: () => void } | null;
  rename: () => void;
  hideTab: (() => void) | null;
  quit: { label: string; run: () => void } | null;
}

/**
 * The verb list for one object, in display order.
 *
 * Refresh applies to all four kinds. It means "reload what the tab is
 * showing" for chat/browser/app, and "reattach the object's persistent
 * session" for a terminal (see ``refreshPanelContent`` in DockviewWorkspace,
 * which is where that distinction actually lives -- this module only fixes
 * that the verb is offered, not what it does).
 * Rename is offered for the kinds whose name is theirs to choose, which is
 * what ``isRenameableKind`` decides and why.
 * Share is an app-only affordance: the share surface is per registered
 * service, and the other three kinds have none. Hide tab and the destructive
 * verb are each omitted per-OBJECT rather than per-kind, through ``actions``,
 * because whether they apply depends on this particular object's state
 * (open or backgrounded, allocated or not) rather than on its kind alone.
 */
export function objectMenuEntries(kind: ObjectMenuKind, actions: ObjectMenuActions): ObjectMenuEntry[] {
  const opening: ObjectMenuEntry[] = [{ label: "Refresh", iconName: "refresh", run: actions.refresh }];
  if (kind === "app" && actions.share !== null) {
    opening.push({ label: actions.share.label, iconName: "share", run: actions.share.run });
  }
  const closing: ObjectMenuEntry[] = [];
  if (isRenameableKind(kind)) {
    closing.push({ label: "Rename", iconName: "edit", run: actions.rename });
  }
  if (actions.hideTab !== null) {
    closing.push({ label: "Hide tab", iconName: "minus", run: actions.hideTab });
  }
  if (actions.quit !== null) {
    closing.push({ label: actions.quit.label, iconName: "power", isDestructive: true, run: actions.quit.run });
  }
  // The divider earns its place only when it has something on both sides: a
  // backgrounded terminal still allocating its session has neither a rename,
  // a tab to hide, nor a destroy, and a menu must not end on a rule.
  return closing.length === 0 ? opening : [...opening, OBJECT_MENU_DIVIDER, ...closing];
}
