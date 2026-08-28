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
 * reloads its iframe, Rename opens the tab's own inline editor, Close tab
 * closes it), while the rail can be showing a *backgrounded* object with no
 * open panel at all, so it needs its own notion of some of the same verbs.
 * ``ObjectMenuActions`` is the seam: every verb's behavior is a callback the
 * caller supplies, and ``objectMenuEntries`` only decides which of those
 * callbacks gets wrapped into a row, in what order, with what icon.
 */

import type { IconName } from "./components/icons";

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

/** A separator between the acting group (Refresh / Share / Add to project)
 *  and the removal group -- see ``objectMenuEntries``. */
export const OBJECT_MENU_DIVIDER = "divider";

/** One row of the menu: an actionable item, or a divider. */
export type ObjectMenuEntry = ObjectMenuItem | typeof OBJECT_MENU_DIVIDER;

/**
 * Whether a kind's name is the user's to choose. Only a chat's is.
 *
 * A chat is an mngr agent: its ref is a stable agent id and its name is
 * separate metadata, so ``mngr rename`` moves the name everywhere the agent is
 * known -- ``mngr list`` included -- without any reference to it moving. Ask
 * an agent to act on the chat by the name you gave it and the name resolves.
 *
 * No other kind has that. A terminal is filed under its live tmux session name
 * and a browser under a Chromium profile directory, so for those the name IS
 * the identity and a rename could only be a display name laid over the top,
 * leaving ``tmux ls``, the profile on disk and every stored ref still saying
 * the old one.
 *
 * An app fails for the opposite reason and just as surely. Its registered
 * service name is a perfectly good stable id -- it keys the member ref,
 * ``apps.toml``, the supervisord program and the ``uv run <name>`` entry point
 * -- so a chosen name laid over it displays fine. But that name is also the
 * only handle anything else on the machine accepts: ``layout.py`` expands a
 * bare word to ``service:<word>``, so an agent told to open the app by the
 * name the user gave it looks up a service that does not exist. A rename the
 * user can read but cannot then refer to is worse than no rename, so apps keep
 * their registered name too.
 *
 * Fixing this properly means teaching ref resolution to accept a display name
 * (and deciding what to do when two objects share one, which nothing currently
 * prevents). That is its own piece of work, and it is the same work that would
 * let terminals and browsers be renamed.
 */
export function isRenameableKind(kind: ObjectMenuKind): boolean {
  return kind === "chat";
}

/**
 * What each verb does for one specific object, supplied by the caller.
 *
 * ``share`` is read only when ``kind`` is "app" (every other kind never
 * offers Share, so a caller building a menu for one of them may simply pass
 * null). ``hideTab``, ``removeFromProject`` and ``quit`` are ``null`` to OMIT
 * the verb, as opposed to wiring it to a no-op. For the first two the null is
 * per-surface -- the dock tab always supplies ``hideTab`` and never
 * ``removeFromProject``, and the rail the other way round (see
 * ``objectMenuEntries`` below), with the rail also passing null
 * ``removeFromProject`` under Everything. ``quit``'s null is per-object: a
 * handful of them -- the workspace's own primary chat, a terminal or browser
 * still allocating its session -- have no destroy available yet.
 * ``share`` and ``quit`` each carry their own
 * label because "Share {name}" / "Quit {name}" always names the object the
 * way the surface calling this currently displays it, and only the caller
 * knows what that is right now.
 *
 * ``addToProjects`` opens the project-picking dialog over the object (also
 * show it in the chosen projects). Null omits it, for a panel with no member
 * ref to file (a terminal still allocating its session).
 *
 * ``stop`` is the reversible process-level verb for a chat: ``mngr stop`` on
 * the agent, which a later message or start brings back. Distinct from the
 * ``quit`` slot below, which for a chat is the confirm-gated delete.
 *
 * ``quit`` is the destructive SLOT, not always a destructive ACT: for a chat,
 * terminal, or browser session it is the confirm-gated "Delete {name}" that
 * ends the object, while for an app it is the reversible service-level
 * "Stop {name}" / "Start {name}" (supervisord stop/start; the row and its
 * memberships stay). The caller says which by ``isDestructive`` -- false swaps
 * the trash icon for the power button and drops the destructive tone, since
 * stopping is one click from undone.
 */
export interface ObjectMenuActions {
  refresh: () => void;
  share: { label: string; run: () => void } | null;
  rename: () => void;
  hideTab: (() => void) | null;
  addToProjects: (() => void) | null;
  removeFromProject: (() => void) | null;
  stop: { label: string; run: () => void } | null;
  quit: { label: string; run: () => void; isDestructive?: boolean } | null;
  /**
   * The SERVICE's own verbs on an app-instance menu -- "Share {app}" and the
   * reversible "Stop {app}" / "Start {app}" -- so the service stays reachable
   * from any of its instances (Everything's rows included). Rendered in the
   * ordinary positions (share with the acting group, lifecycle with the
   * process verbs), not as a group of their own. Null (or absent) everywhere
   * else: a chat, terminal, or browser has no service behind it, and a bare
   * app row IS the service, so its share and lifecycle ride the ordinary
   * slots instead.
   */
  serviceGroup?: {
    share: { label: string; run: () => void } | null;
    lifecycle: { label: string; run: () => void } | null;
  } | null;
}

/**
 * The verb list for one object, in display order.
 *
 * The menu reads as two groups. The opening group acts on the object --
 * Refresh, Share, Add to project. The closing group removes, in increasing
 * severity: Close tab drops the panel, Remove from project drops the filing,
 * Stop drops the process, Delete drops the object -- reading them in that
 * order is what tells the easily-confused acts apart. Rename leads the
 * closing group for the one kind that has it (``isRenameableKind``).
 *
 * Refresh applies to all four kinds. It means "reload what the tab is
 * showing" for chat/browser/app, and "reattach the object's persistent
 * session" for a terminal (see ``refreshPanelContent`` in DockviewWorkspace,
 * which is where that distinction actually lives -- this module only fixes
 * that the verb is offered, not what it does).
 * Share is an app-only affordance: the share surface is per registered
 * service, and the other three kinds have none. It renders from whichever
 * slot supplied it -- the bare app's own ``share`` or the instance menu's
 * ``serviceGroup`` -- and likewise the process verb comes from ``stop`` (a
 * chat) or ``serviceGroup.lifecycle`` (an app instance's service).
 * Close tab and Remove from project are each one SURFACE's job, which the
 * two callers say by supplying only their own: closing is what you want
 * while looking at the tab, and unfiling is what you want while looking at
 * the project's list of what it shows.
 * The destructive verb is omitted per-OBJECT rather than per-kind, through
 * ``actions``: whether it applies depends on this object's state (allocated
 * or not, the primary agent or not) rather than on its kind.
 */
export function objectMenuEntries(kind: ObjectMenuKind, actions: ObjectMenuActions): ObjectMenuEntry[] {
  const opening: ObjectMenuEntry[] = [{ label: "Refresh", iconName: "refresh", run: actions.refresh }];
  const share = kind === "app" ? (actions.share ?? actions.serviceGroup?.share ?? null) : null;
  if (share != null) {
    opening.push({ label: share.label, iconName: "user-plus", run: share.run });
  }
  if (actions.addToProjects !== null) {
    opening.push({ label: "Add to project...", iconName: "folder-plus", run: actions.addToProjects });
  }
  const closing: ObjectMenuEntry[] = [];
  if (isRenameableKind(kind)) {
    closing.push({ label: "Rename", iconName: "edit", run: actions.rename });
  }
  if (actions.hideTab !== null) {
    closing.push({ label: "Close tab", iconName: "close", run: actions.hideTab });
  }
  if (actions.removeFromProject !== null) {
    closing.push({ label: "Remove from project", iconName: "minus-circle", run: actions.removeFromProject });
  }
  // Above the delete it must never be confused with: stopping puts the process
  // down and keeps the object, deleting ends it. The icons say the same thing
  // (power button vs trash can).
  const stop = actions.stop ?? actions.serviceGroup?.lifecycle ?? null;
  if (stop != null) {
    closing.push({ label: stop.label, iconName: "power", run: stop.run });
  }
  if (actions.quit !== null) {
    const isDestructive = actions.quit.isDestructive !== false;
    closing.push({
      label: actions.quit.label,
      iconName: isDestructive ? "trash" : "power",
      isDestructive,
      run: actions.quit.run,
    });
  }
  // The divider earns its place only when it has something on both sides: a
  // backgrounded terminal still allocating its session has neither a rename,
  // a tab to close, nor a destroy, and a menu must not end on a rule.
  return closing.length === 0 ? opening : [...opening, OBJECT_MENU_DIVIDER, ...closing];
}
