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
 * Built on the shared Modal shell (views/Modal.ts): its backdrop-mousedown
 * dismissal, and a document-level Escape wired through the shell's `overlay`
 * hook (as ProjectSettingsModal does, and for the same reason: focus sits on a
 * checkbox row as easily as anywhere).
 */

import m from "mithril";
import { Modal } from "./Modal";
import type { ProjectInfo } from "../models/Projects";
import { squiggleMarkup } from "./squiggles";
import { MODAL_MESSAGE_CLASS, buttonClass } from "./primitives";

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
    const isFixed = attrs.memberProjectIds.includes(project.project_id);
    const isChecked = isFixed || selected.has(project.project_id);
    return m(
      "label",
      {
        class:
          "flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 hover:bg-fill-hover " +
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
        m("span", { class: "min-w-0 flex-1 truncate text-[13px] text-primary" }, project.name),
        isFixed ? m("span", { class: "shrink-0 text-[11px] text-faint" }, "already added") : null,
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
        Modal,
        {
          onDismiss: attrs.onCancel,
          overlay: {
            oncreate() {
              document.addEventListener("keydown", handleKeydown);
            },
            onremove() {
              document.removeEventListener("keydown", handleKeydown);
            },
          },
          title: "Add to project",
          actions: [
            m("button", { class: buttonClass("secondary"), onclick: attrs.onCancel }, "Cancel"),
            m(
              "button",
              {
                class: buttonClass("primary"),
                disabled: selected.size === 0,
                onclick() {
                  attrs.onConfirm([...selected]);
                },
              },
              "Add",
            ),
          ],
        },
        [
          m("p", { class: MODAL_MESSAGE_CLASS }, [
            "Choose the projects that should also show ",
            m("strong", attrs.memberLabel),
            ".",
          ]),
          attrs.projects.length === 0
            ? m("p", { class: "py-2 text-[13px] text-faint" }, "There are no projects on this machine yet.")
            : m(
                "div",
                { class: "mb-3 flex max-h-[40vh] flex-col gap-0.5 overflow-y-auto" },
                attrs.projects.map((project) => projectRow(attrs, project)),
              ),
        ],
      );
    },
  };
}
