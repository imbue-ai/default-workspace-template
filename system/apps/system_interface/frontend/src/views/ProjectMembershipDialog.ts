/**
 * Dialog for filing one instance's address into more projects' tab sets, reached
 * from the tab menu's "Add to project..." row.
 *
 * A checkbox list of every project on the machine: the ones already showing
 * the instance render checked and fixed (adding never removes -- taking the
 * instance out of a project is its rail row's "Remove from project"), and
 * confirming adds the address to the newly checked rest. Everything is not
 * offered: it is the home, lists the whole machine, and an instance leaves it
 * only by being deleted.
 *
 * Built on the shared Modal shell (components/Modal.ts): its backdrop-mousedown dismissal, and
 * Escape through the shell's `onEscape` rather than a key handler on any one control, because
 * focus sits on a checkbox row as easily as anywhere.
 */

import m from "mithril";
import { Button } from "./components/Button";
import { MODAL_MESSAGE_CLASS, Modal } from "./components/Modal";
import type { ProjectInfo } from "../models/Inventory";
import { squiggleMarkup } from "./squiggles";

const ROW_GLYPH_SIZE = 16;

export interface ProjectMembershipDialogAttrs {
  // What the instance is currently titled, for the dialog copy.
  instanceLabel: string;
  // Every project on the machine (Everything is never in here).
  projects: readonly ProjectInfo[];
  // Projects whose tab set already holds the instance, by id.
  showingProjectIds: readonly string[];
  // Fired with the newly checked project ids. The caller files the address into
  // each and closes.
  onConfirm: (selectedProjectIds: string[]) => void;
  onCancel: () => void;
}

export function ProjectMembershipDialog(): m.Component<ProjectMembershipDialogAttrs> {
  const selected = new Set<string>();

  function projectRow(attrs: ProjectMembershipDialogAttrs, project: ProjectInfo): m.Vnode {
    // A project already showing the object is settled: its box stays checked
    // and fixed, saying "already here" rather than offering a removal this
    // dialog does not do.
    const isFixed = attrs.showingProjectIds.includes(project.id);
    const isChecked = isFixed || selected.has(project.id);
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
        m("span", { class: "min-w-0 flex-1 truncate text-(length:--font-size-row) text-primary" }, project.name),
        isFixed ? m("span", { class: "shrink-0 text-[11px] text-faint" }, "already added") : null,
      ],
    );
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      return m(
        Modal,
        {
          onDismiss: attrs.onCancel,
          onEscape: attrs.onCancel,
          title: "Add to project",
          actions: [
            m(Button, { onclick: attrs.onCancel }, "Cancel"),
            m(
              Button,
              {
                variant: "primary",
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
            m("strong", attrs.instanceLabel),
            ".",
          ]),
          attrs.projects.length === 0
            ? m(
                "p",
                { class: "py-2 text-(length:--font-size-row) text-faint" },
                "There are no projects on this machine yet.",
              )
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
