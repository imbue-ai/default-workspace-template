/**
 * The rail's "All apps" popover: every app on the machine whose primary action the active
 * project has not already pinned to its rail.
 *
 * Clicking a row runs the action. Hovering one reveals a pin toggle that adds the (app,
 * action) shortcut to the project's rail; the popover stays open afterward so several can be
 * pinned in one visit, and the row just pinned collapses and fades for one transition's worth
 * of time rather than snapping a different app under a pointer that has not moved.
 *
 * Everything pins nothing -- its rail already shows a fixed row for every openable app -- so
 * under it the popover lists every app with no toggles.
 *
 * The popover renders as a bare card and is placed by the rail, which owns the one
 * floating-menu placement every one of its menus uses.
 */

import m from "mithril";
import type { AppAction, AppRecord } from "../models/Inventory";
import { appStoppedDetail, getOpenableApps, primaryActionForApp } from "../models/Inventory";
import { appIconMarkup } from "./appIcon";
import { hoverTooltipAttrs } from "./hoverTooltip";
import { icon } from "./icons";

const FILTER_ROW_THRESHOLD = 8;
const ROW_GLYPH_SIZE = 14;
const XMLNS = "http://www.w3.org/2000/svg";
const PIN_PATH = '<path d="M9 4h6l-1 5 3 3v2H7v-2l3-3-1-5z"/><line x1="12" y1="14" x2="12" y2="20"/>';
const ROW_FADE_DURATION_MS = 150;

function pinIcon(): string {
  return (
    `<svg xmlns="${XMLNS}" width="14" height="14" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="2" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PIN_PATH}</svg>`
  );
}

/** One row the popover can offer: an app and its primary action. */
export interface PickableAction {
  app: AppRecord;
  action: AppAction;
}

/** Every app the popover can offer with its primary action, ordered by display name. */
export function pickableActions(apps: readonly AppRecord[]): PickableAction[] {
  return apps
    .flatMap((app) => {
      const action = primaryActionForApp(app);
      return action === null ? [] : [{ app, action }];
    })
    .sort((a, b) => a.app.display_name.localeCompare(b.app.display_name));
}

/** Case-insensitive substring match on either name an app answers to. */
export function filterActions(rows: readonly PickableAction[], query: string): PickableAction[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return [...rows];
  return rows.filter(
    (row) => row.app.name.toLowerCase().includes(needle) || row.app.display_name.toLowerCase().includes(needle),
  );
}

export function pinKey(row: PickableAction): string {
  return `${row.app.name}:${row.action.id}`;
}

/** The rows a project has not already pinned. */
export function unpinnedActions(rows: readonly PickableAction[], pinnedKeys: readonly string[]): PickableAction[] {
  const pinned = new Set(pinnedKeys);
  return rows.filter((row) => !pinned.has(pinKey(row)));
}

export interface AllAppsPickerAttrs {
  // The active project's display name, null under Everything (which pins nothing).
  projectName: string | null;
  // The active project's shortcuts as ``<app>:<action>`` keys, which this popover excludes.
  pinnedKeys: readonly string[];
  // Run this app's action in the active view. The rail closes the popover.
  onRunAction: (app: AppRecord, action: AppAction) => void;
  // Pin this action to the active project's rail. Never called under Everything.
  onPin: (app: AppRecord, action: AppAction) => void;
}

export function AllAppsPicker(): m.Component<AllAppsPickerAttrs> {
  let filterText = "";
  const fadingKeys = new Set<string>();

  function appRow(row: PickableAction, isPinnable: boolean, isFadingOut: boolean, attrs: AllAppsPickerAttrs): m.Vnode {
    const label = row.app.display_name;
    const isStopped = !row.app.is_running;
    return m(
      "div",
      {
        key: pinKey(row),
        "data-app": row.app.name,
        class:
          "project-rail-app group flex w-full items-center gap-2 px-3 text-left " +
          (isStopped ? "project-rail-app-stopped text-text-faint " : "text-text-primary ") +
          "transition-all duration-150 " +
          (isFadingOut ? "h-0 overflow-hidden opacity-0" : "h-8 cursor-pointer opacity-100 hover:bg-bg-hover"),
        ...(isStopped && !isFadingOut ? hoverTooltipAttrs(`${label} — ${appStoppedDetail(row.app)}`) : {}),
        onclick: isFadingOut ? undefined : () => attrs.onRunAction(row.app, row.action),
      },
      [
        m(
          "span",
          { class: "flex shrink-0 items-center text-text-faint" },
          m.trust(
            appIconMarkup(row.app.icon, ROW_GLYPH_SIZE, icon("external-link", { size: ROW_GLYPH_SIZE }), row.app.name),
          ),
        ),
        m("span", { class: "min-w-0 flex-1 truncate" }, label),
        !isPinnable || isFadingOut
          ? null
          : m(
              "button",
              {
                type: "button",
                class:
                  "project-rail-pin flex h-5 w-5 shrink-0 cursor-pointer items-center justify-center rounded " +
                  "text-text-faint opacity-0 hover:text-text-primary focus-visible:opacity-100 " +
                  "group-hover:opacity-100",
                "aria-label": `Pin ${label}`,
                ...hoverTooltipAttrs("Pin it to this project's rail. What it opens does not change."),
                onclick: (event: MouseEvent) => {
                  event.stopPropagation();
                  const key = pinKey(row);
                  fadingKeys.add(key);
                  attrs.onPin(row.app, row.action);
                  setTimeout(() => {
                    fadingKeys.delete(key);
                    m.redraw();
                  }, ROW_FADE_DURATION_MS);
                },
              },
              m.trust(pinIcon()),
            ),
      ],
    );
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const all = pickableActions(getOpenableApps());
      const matching = filterActions(all, filterText);
      const isPinnable = attrs.projectName !== null;
      const rows = isPinnable
        ? unpinnedActions(
            matching,
            attrs.pinnedKeys.filter((key) => !fadingKeys.has(key)),
          )
        : matching;
      const query = filterText.trim();
      const emptyMessage: m.Children =
        all.length === 0
          ? [
              m("p", "No apps are running on this machine."),
              m("p", { class: "mt-2" }, "Tell Minds to create one via chat!"),
            ]
          : matching.length === 0
            ? `No apps match "${query}".`
            : query === ""
              ? "Every app on this machine is already pinned here."
              : `Every app matching "${query}" is already pinned here.`;

      return m("div", { class: "flex max-h-[60vh] w-[240px] flex-col" }, [
        all.length > FILTER_ROW_THRESHOLD
          ? m("input", {
              type: "text",
              class:
                "mx-3 my-1 h-7 shrink-0 rounded-md bg-bg-sidebar px-2 text-[13px] text-text-primary outline-none " +
                "placeholder:text-text-faint",
              value: filterText,
              placeholder: "Filter apps",
              autofocus: true,
              oninput(event: InputEvent) {
                filterText = (event.target as HTMLInputElement).value;
              },
              onkeydown(event: KeyboardEvent) {
                if (event.key !== "Enter") return;
                if (rows.length > 0) attrs.onRunAction(rows[0].app, rows[0].action);
              },
            })
          : null,
        rows.length === 0
          ? m("div", { class: "px-3 py-2 text-[13px] text-text-faint" }, emptyMessage)
          : m(
              "div",
              { class: "min-h-0 flex-1 overflow-y-auto" },
              rows.map((row) => appRow(row, isPinnable, fadingKeys.has(pinKey(row)), attrs)),
            ),
      ]);
    },
  };
}
