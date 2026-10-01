/**
 * The phone's start sheet (plan-phone-interface.md): the launcher's menu made tappable. The field takes the
 * launcher's placeholder and is not focused on open (a keyboard rising over the rows would hide them); its rows
 * are the launcher's own, from ``reducers/launcherRows``, with the free-text rows tapped rather than chorded.
 * Enter runs the highlighted row, as the launcher's field does.
 */

import m from "mithril";
import type { LauncherMenuRows, LauncherRow } from "../../reducers/launcherRows";
import { LAUNCHER_PLACEHOLDER, isLineBreakChord } from "../LauncherField";
import { LauncherSections } from "../LauncherMenu";
import { Sheet } from "./Sheet";

export const START_SHEET_TITLE = "Open something";

export interface StartSheetAttrs {
  readonly menu: LauncherMenuRows;
  readonly highlightIndex: number;
  readonly query: string;
  readonly onQuery: (query: string) => void;
  readonly onRun: (row: LauncherRow) => void;
  readonly onHighlight: (index: number) => void;
  readonly onRunHighlight: () => void;
  readonly onDismiss: () => void;
}

export const StartSheet: m.Component<StartSheetAttrs> = {
  view(vnode) {
    const { menu, highlightIndex, query, onQuery, onRun, onHighlight, onRunHighlight, onDismiss } = vnode.attrs;
    return m(
      Sheet,
      {
        name: "start",
        onDismiss,
        head: m("h2", { class: "m-0 px-4 pt-1.5 pb-2.5 text-lg font-bold text-primary" }, START_SHEET_TITLE),
      },
      [
        m(
          "div",
          {
            class:
              "mx-3 mb-1.5 flex min-h-11 shrink-0 items-center rounded-xl border border-strong bg-surface px-3 " +
              "focus-within:border-accent",
          },
          m("textarea", {
            "data-phone-start-field": "",
            rows: 1,
            enterkeyhint: "go",
            "aria-label": LAUNCHER_PLACEHOLDER,
            placeholder: LAUNCHER_PLACEHOLDER,
            value: query,
            class:
              "max-h-30 min-w-0 flex-1 resize-none bg-transparent py-2.5 leading-5 text-(length:--font-size-body) " +
              "text-primary outline-none placeholder:text-faint",
            oninput: (event: InputEvent) => onQuery((event.target as HTMLTextAreaElement).value),
            onkeydown: (event: KeyboardEvent) => {
              if (event.key !== "Enter" || isLineBreakChord(event) || event.isComposing) return;
              event.preventDefault();
              onRunHighlight();
            },
          }),
        ),
        m(
          "div",
          {
            role: "menu",
            class: "phone-start-rows min-h-0 flex-1 overflow-y-auto pt-1 pb-(--desk-phone-safe-bottom)",
          },
          m(LauncherSections, {
            menu,
            highlightIndex,
            isApplePlatform: false,
            isSheet: true,
            onRun,
            onHighlight,
            onAppShortcutContextMenu: null,
          }),
        ),
      ],
    );
  },
};
