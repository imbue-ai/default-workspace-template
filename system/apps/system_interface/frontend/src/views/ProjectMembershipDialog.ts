/**
 * Dialog for filing one machine object into projects, reached from the object
 * menu's "Add to project..." and "Move to project..." rows.
 *
 * Both modes present the same checkbox list of every project on the machine;
 * what confirming means is the only difference. Add: also show the object in
 * the newly checked projects (the ones already showing it render checked and
 * fixed -- adding never removes). Move: show the object in exactly the checked
 * projects, leaving every unchecked one -- so the current memberships start
 * checked and every row is editable. Everything is not offered: it is the
 * home, lists the whole machine, and an object leaves it only by being
 * deleted.
 *
 * Shell and class names follow the shared `.custom-url-dialog` markup, with
 * the shared backdrop-mousedown dismissal and a document-level Escape (as
 * ProjectSettingsModal does, and for the same reason: focus sits on a
 * checkbox row as easily as anywhere).
 */

import m from "mithril";
import { backdropDismissAttrs } from "./modalBackdrop";
import type { ProjectInfo } from "../models/Projects";
import { squiggleMarkup } from "./squiggles";

const ROW_GLYPH_SIZE = 16;

export interface ProjectMembershipDialogAttrs {
  // What the object is currently called, for the dialog copy.
  memberLabel: string;
  mode: "add" | "move";
  // Every project on the machine (Everything is never in here).
  projects: readonly ProjectInfo[];
  // Projects currently showing the object, by id.
  memberProjectIds: readonly string[];
  // Fired with the checked project ids. The caller applies the difference
  // (add: additions only; move: additions and removals) and closes.
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

  /** Whether confirming would change anything: at least one addition, or (in
   *  move mode) at least one removal. A confirm that would do nothing stays
   *  disabled rather than pretending to act. */
  function hasChanges(attrs: ProjectMembershipDialogAttrs): boolean {
    const current = new Set(attrs.memberProjectIds);
    const hasAdditions = [...selected].some((id) => !current.has(id));
    if (attrs.mode === "add") return hasAdditions;
    const hasRemovals = attrs.memberProjectIds.some((id) => !selected.has(id));
    return hasAdditions || hasRemovals;
  }

  function projectRow(attrs: ProjectMembershipDialogAttrs, project: ProjectInfo): m.Vnode {
    const isMember = attrs.memberProjectIds.includes(project.project_id);
    // Adding never removes, so in add mode a project already showing the
    // object is settled: its box stays checked and fixed, saying "already
    // here" rather than offering a removal this dialog does not do.
    const isFixed = attrs.mode === "add" && isMember;
    const isChecked = isFixed || selected.has(project.project_id);
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
              selected.add(project.project_id);
            } else {
              selected.delete(project.project_id);
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
      // Move mode starts from the current memberships, since confirming
      // declares the full wanted set; add mode starts empty, since the
      // current memberships are fixed rows rather than part of the selection.
      if (vnode.attrs.mode === "move") {
        for (const id of vnode.attrs.memberProjectIds) selected.add(id);
      }
    },

    view(vnode) {
      const attrs = vnode.attrs;
      latestAttrs = attrs;
      const explanation =
        attrs.mode === "add"
          ? ["Choose the projects that should also show ", m("strong", attrs.memberLabel), "."]
          : [
              "Choose the projects that should show ",
              m("strong", attrs.memberLabel),
              ". It leaves any project left unchecked.",
            ];

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
            m("h3.custom-url-dialog-title", attrs.mode === "add" ? "Add to project" : "Move to project"),
            m("p.destroy-dialog-message", explanation),
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
                  disabled: !hasChanges(attrs),
                  onclick() {
                    attrs.onConfirm([...selected]);
                  },
                },
                attrs.mode === "add" ? "Add" : "Move",
              ),
            ]),
          ]),
        ],
      );
    },
  };
}
