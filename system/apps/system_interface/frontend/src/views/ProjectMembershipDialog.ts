/**
 * Dialog for filing one machine object into more projects, reached from the
 * object menu's "Add to project..." row.
 *
 * A checkbox list of every project on the machine: the ones already showing
 * the object render checked and fixed (adding never removes -- taking the
 * object out of a project is its rail row's "Remove from project"), and
 * confirming adds the object to the newly checked rest. Everything is not
 * offered: it is the home, lists the whole machine, and an object leaves it
 * only by being deleted.
 *
 * Shell and class names follow the shared `.custom-url-dialog` markup, with
 * the shared backdrop-mousedown dismissal and a document-level Escape (as
 * ProjectSettingsModal does, and for the same reason: focus sits on a
 * checkbox row as easily as anywhere).
 */

import m from "mithril";
import { backdropDismissAttrs } from "./modalBackdrop";
import type { ProjectInfo } from "../models/Inventory";
import { squiggleMarkup } from "./squiggles";

const ROW_GLYPH_SIZE = 16;

export interface ProjectMembershipDialogAttrs {
  // What the object is currently called, for the dialog copy.
  memberLabel: string;
  // Every project on the machine (Everything is never in here).
  projects: readonly ProjectInfo[];
  // Projects currently showing the object, by id.
  memberProjectIds: readonly string[];
  // Fired with the newly checked project ids. The caller adds the object to
  // each and closes.
  onConfirm: (selectedProjectIds: string[]) => void;
  onCancel: () => void;
}

export function ProjectMembershipDialog(): m.Component<ProjectMembershipDialogAttrs> {
  const selected = new Set<string>();
  let latestAttrs: ProjectMembershipDialogAttrs | null = null;

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape" || latestAttrs === null) return;
    latestAttrs.onCancel();
    m.redraw();
  }

  function projectRow(attrs: ProjectMembershipDialogAttrs, project: ProjectInfo): m.Vnode {
    // A project already showing the object is settled: its box stays checked
    // and fixed, saying "already here" rather than offering a removal this
    // dialog does not do.
    const isFixed = attrs.memberProjectIds.includes(project.id);
    const isChecked = isFixed || selected.has(project.id);
    return m(
      "label",
      {
        class:
          "flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 hover:bg-bg-hover " +
          (isFixed ? "cursor-default opacity-60" : ""),
      },
      [
        m("input", {
          type: "checkbox",
          checked: isChecked,
          disabled: isFixed,
          "aria-label": project.name,
          onchange(e: Event) {
            if ((e.target as HTMLInputElement).checked) {
              selected.add(project.id);
            } else {
              selected.delete(project.id);
            }
          },
        }),
        m(
          "span",
          { class: "flex h-4 w-4 shrink-0 items-center justify-center" },
          m.trust(squiggleMarkup(project.glyph, project.color, ROW_GLYPH_SIZE)),
        ),
        m("span", { class: "min-w-0 flex-1 truncate text-[13px] text-text-primary" }, project.name),
        isFixed ? m("span", { class: "shrink-0 text-[11px] text-text-faint" }, "already added") : null,
      ],
    );
  }

  return {
    oninit(vnode) {
      latestAttrs = vnode.attrs;
    },

    view(vnode) {
      const attrs = vnode.attrs;
      latestAttrs = attrs;

      return m(
        "div.custom-url-dialog-overlay",
        {
          oncreate() {
            document.addEventListener("keydown", handleKeydown);
          },
          onremove() {
            document.removeEventListener("keydown", handleKeydown);
          },
          ...backdropDismissAttrs(attrs.onCancel),
        },
        [
          m("div.custom-url-dialog", [
            m("h3.custom-url-dialog-title", "Add to project"),
            m("p.destroy-dialog-message", [
              "Choose the projects that should also show ",
              m("strong", attrs.memberLabel),
              ".",
            ]),
            attrs.projects.length === 0
              ? m("p", { class: "py-2 text-[13px] text-text-faint" }, "There are no projects on this machine yet.")
              : m(
                  "div",
                  { class: "mb-3 flex max-h-[40vh] flex-col gap-0.5 overflow-y-auto" },
                  attrs.projects.map((project) => projectRow(attrs, project)),
                ),
            m("div.custom-url-dialog-actions", [
              m("button.custom-url-dialog-cancel", { onclick: attrs.onCancel }, "Cancel"),
              m(
                "button.custom-url-dialog-open",
                {
                  disabled: selected.size === 0,
                  onclick() {
                    attrs.onConfirm([...selected]);
                  },
                },
                "Add",
              ),
            ]),
          ]),
        ],
      );
    },
  };
}
