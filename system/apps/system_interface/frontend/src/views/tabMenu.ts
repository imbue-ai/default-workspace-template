/**
 * The verb set for one instance, defined ONCE so the tab's kebab menu and the rail's row menu
 * render the identical list off the identical rule.
 *
 * What varies **by instance** (whether it can be renamed, deleted, whether its app can be
 * stopped) is read off the inventory records here. What varies **by caller** -- what running
 * a verb actually does -- is not: the tab acts on a live, open panel (Close tab), while the
 * rail can be showing a backgrounded instance with no open panel at all (Remove from project).
 * ``TabMenuActions`` is the seam: every verb's behavior is a callback the caller supplies,
 * and ``tabMenuEntries`` only decides which of those gets wrapped into a row, in what order.
 */

import type { AppRecord, InstanceRecord } from "../models/Inventory";
import { isAppStoppable } from "../models/Inventory";
import type { IconName } from "./icons";

/** One actionable row. */
export interface TabMenuItem {
  label: string;
  iconName: IconName;
  isDestructive?: boolean;
  run: () => void;
}

/** A separator between the acting group and the removal group. */
export const TAB_MENU_DIVIDER = "divider";

export type TabMenuEntry = TabMenuItem | typeof TAB_MENU_DIVIDER;

/**
 * What each verb does for one specific instance, supplied by the caller. A ``null`` OMITS the
 * verb: the tab supplies ``closeTab`` and never ``removeFromProject``, the rail the other way
 * round (and null under Everything, which nothing can be removed from). ``share`` is null
 * when there is no embedder share surface for the app.
 */
export interface TabMenuActions {
  refresh: () => void;
  share: (() => void) | null;
  addToProjects: () => void;
  rename: () => void;
  closeTab: (() => void) | null;
  removeFromProject: (() => void) | null;
  setAppLifecycle: (action: "stop" | "start") => void;
  delete: () => void;
}

/**
 * The verb list for one instance, in display order.
 *
 * The menu reads as two groups. The opening group acts on the instance -- Refresh, Share, Add
 * to project. The closing group removes, in increasing severity: Rename (for the instances
 * their app renames), Close tab drops the panel, Remove from project drops the filing, Stop
 * drops the app's process, Delete drops the instance.
 *
 * Delete is offered only for an instance of an app that has instances: a single-instance app
 * IS its one record, and the record goes only when the app is unregistered. Stop and Start
 * are offered only for an app the workspace can honestly stop (supervised, not critical, and
 * not inside a critical app's program), and read from the app's liveness.
 */
export function tabMenuEntries(app: AppRecord, instance: InstanceRecord, actions: TabMenuActions): TabMenuEntry[] {
  const opening: TabMenuEntry[] = [{ label: "Refresh", iconName: "refresh", run: actions.refresh }];
  if (actions.share !== null) {
    opening.push({ label: `Share ${app.display_name}`, iconName: "user-plus", run: actions.share });
  }
  opening.push({ label: "Add to project...", iconName: "folder-plus", run: actions.addToProjects });
  const closing: TabMenuEntry[] = [];
  if (instance.renameable) {
    closing.push({ label: "Rename", iconName: "edit", run: actions.rename });
  }
  if (actions.closeTab !== null) {
    closing.push({ label: "Close tab", iconName: "close", run: actions.closeTab });
  }
  if (actions.removeFromProject !== null) {
    closing.push({ label: "Remove from project", iconName: "minus-circle", run: actions.removeFromProject });
  }
  if (isAppStoppable(app)) {
    const action = app.is_running ? "stop" : "start";
    closing.push({
      label: `${action === "stop" ? "Stop" : "Start"} ${app.display_name}`,
      iconName: "power",
      run: () => actions.setAppLifecycle(action),
    });
  }
  if (app.has_instances) {
    closing.push({ label: `Delete ${instance.title}`, iconName: "trash", isDestructive: true, run: actions.delete });
  }
  return closing.length === 0 ? opening : [...opening, TAB_MENU_DIVIDER, ...closing];
}
