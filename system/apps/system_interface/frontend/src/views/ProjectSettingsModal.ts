/**
 * Modal for one project's display metadata: its name, its color and which of
 * the ten squiggles stands for it -- plus the one place a project can be
 * deleted.
 *
 * Reached from the sidebar's switcher header context menu, and only ever for a
 * real project. Creating no longer goes through here: the switcher's "New
 * project" mints "Project N" with the next unused glyph on the spot, so the
 * user lands in the New Tab launcher instead of on a form. Everything never
 * reaches here either -- it is a view rather than a project, with no name,
 * color, glyph or member list of its own and nothing to delete.
 *
 * Deleting is confirm-gated in place -- a second, red button inside this same
 * dialog rather than a second stacked dialog -- because the modal already owns
 * the screen and the name being deleted is right there in the preview.
 * Deleting a project is itself a pure view operation now: it removes the
 * project's own view and member list and nothing more, so there is no longer
 * any consequence for the confirmation to enumerate beyond that.
 *
 * Built on the shared Modal shell (views/Modal.ts): a backdrop mousedown to
 * dismiss, Enter in the name field to save. Escape is listened for on the
 * document (wired through the shell's `overlay` hook, as FastModeModal does)
 * rather than on the name field alone, because focus here just as easily sits
 * on a swatch or a glyph.
 */

import m from "mithril";
import { Modal } from "./Modal";
import { deleteProjectRequest, updateProjectSettings } from "../models/Projects";
import type { ProjectInfo } from "../models/Projects";
import { SQUIGGLE_GLYPHS, squiggleMarkup } from "./squiggles";
import { Button } from "./Button";
import { MODAL_LABEL_CLASS, MODAL_MESSAGE_CLASS, inputClass } from "./primitives";

export interface ProjectSettingsModalAttrs {
  project: ProjectInfo;
  // Fired with the server's copy of the project once the save lands, so the
  // parent can close the modal and adopt the stored values (the server
  // normalizes the name).
  onSaved: (project: ProjectInfo) => void;
  // Fired once the project is gone server-side. The parent re-lists; a client
  // mounted on the deleted project is moved off it by the `project_deleted`
  // broadcast, the same path another client's delete takes.
  onDeleted: (projectId: string) => void;
  onCancel: () => void;
}

// The palette is exactly the glyphs' own signature colors, so every project
// color belongs to the family the squiggles were drawn in.
const PALETTE: readonly string[] = SQUIGGLE_GLYPHS.map((glyph) => glyph.color);

const PREVIEW_GLYPH_SIZE = 40;
const PICKER_GLYPH_SIZE = 28;

// `squiggleMarkup` wraps out-of-range indices on its own, but the picker
// compares indices to decide which cell is selected, so a glyph read back from
// the server is normalized once on the way in.
function normalizedGlyphIndex(glyph: number): number {
  const count = SQUIGGLE_GLYPHS.length;
  return ((Math.trunc(glyph) % count) + count) % count;
}

export function ProjectSettingsModal(): m.Component<ProjectSettingsModalAttrs> {
  let name = "";
  let color = SQUIGGLE_GLYPHS[0].color;
  let glyphIndex = 0;
  let isSaving = false;
  let isDeleting = false;
  let isConfirmingDelete = false;
  let error: string | null = null;
  // The Escape handler is registered on the document once, so it reaches the
  // callbacks through this rather than through a vnode captured at create time.
  let latestAttrs: ProjectSettingsModalAttrs | null = null;

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape" || latestAttrs === null) return;
    // Escape undoes the delete confirmation first: it should back out of the
    // step the user just took, not close the whole modal from under it.
    if (isConfirmingDelete) {
      isConfirmingDelete = false;
    } else {
      latestAttrs.onCancel();
    }
    m.redraw();
  }

  async function save(attrs: ProjectSettingsModalAttrs): Promise<void> {
    const chosen = name.trim();
    if (!chosen || isSaving || isDeleting) return;
    isSaving = true;
    error = null;
    m.redraw();

    try {
      attrs.onSaved(await updateProjectSettings(attrs.project.project_id, chosen, color, glyphIndex));
    } catch (e) {
      error = (e as Error).message ?? "The project could not be saved.";
      isSaving = false;
    }
    m.redraw();
  }

  async function deleteProject(attrs: ProjectSettingsModalAttrs): Promise<void> {
    if (isSaving || isDeleting) return;
    isDeleting = true;
    error = null;
    m.redraw();

    try {
      await deleteProjectRequest(attrs.project.project_id);
      attrs.onDeleted(attrs.project.project_id);
    } catch (e) {
      // Stay in the confirmation step: the reason lands next to the button that
      // failed, and a retry is one click away rather than two.
      error = (e as Error).message ?? "The project could not be deleted.";
      isDeleting = false;
    }
    m.redraw();
  }

  function colorSwatch(swatch: string): m.Vnode {
    const isSelected = swatch === color;
    return m("button", {
      class: "h-6 w-6 cursor-pointer rounded-full border-0 p-0",
      style:
        `background: ${swatch}; outline-offset: 2px; ` +
        `outline: ${isSelected ? "2px solid var(--c-text-primary)" : "none"};`,
      title: swatch,
      "aria-label": `Color ${swatch}`,
      "aria-pressed": isSelected ? "true" : "false",
      onclick() {
        color = swatch;
      },
    });
  }

  function glyphCell(index: number): m.Vnode {
    const isSelected = index === glyphIndex;
    return m(
      "button",
      {
        class: "flex h-12 cursor-pointer items-center justify-center rounded-md border bg-transparent",
        // The selection ring is a shadow rather than a thicker border, so
        // picking a glyph never nudges the grid.
        style:
          `border-color: ${isSelected ? color : "var(--c-border)"};` +
          (isSelected ? ` box-shadow: 0 0 0 1px ${color};` : ""),
        "aria-label": `Squiggle ${index + 1}`,
        "aria-pressed": isSelected ? "true" : "false",
        onclick() {
          glyphIndex = index;
        },
      },
      // Every glyph previews in the chosen color, so the grid shows what the
      // project would actually look like rather than ten unrelated hues.
      m.trust(squiggleMarkup(index, color, PICKER_GLYPH_SIZE)),
    );
  }

  // The delete step's copy, shown at the foot of the body while confirming; its
  // Keep/Delete buttons ride the shell's action row (see the view).
  function deleteConfirmationMessage(attrs: ProjectSettingsModalAttrs): m.Vnode {
    return m("p", { class: MODAL_MESSAGE_CLASS }, [
      "Delete ",
      m("strong", attrs.project.name),
      "? This removes the view only — everything it shows keeps running, and stays in Everything and " +
        "in any other project showing it.",
    ]);
  }

  function deleteConfirmationActions(attrs: ProjectSettingsModalAttrs): m.Children {
    return [
      m(
        Button,
        {
          disabled: isDeleting,
          onclick() {
            isConfirmingDelete = false;
          },
        },
        "Keep project",
      ),
      m(
        Button,
        {
          variant: "destructive",
          extra: "destroy-dialog-btn-destroy",
          disabled: isDeleting,
          onclick: () => deleteProject(attrs),
        },
        isDeleting ? "Deleting..." : "Delete project",
      ),
    ];
  }

  function editActions(attrs: ProjectSettingsModalAttrs, trimmedName: string): m.Children {
    return [
      m(
        Button,
        {
          extra: "destroy-dialog-btn-cancel mr-auto",
          disabled: isSaving || isDeleting,
          onclick() {
            isConfirmingDelete = true;
          },
        },
        "Delete",
      ),
      m(Button, { onclick: attrs.onCancel, disabled: isSaving || isDeleting }, "Cancel"),
      m(
        Button,
        {
          variant: "primary",
          onclick: () => save(attrs),
          disabled: isSaving || isDeleting || !trimmedName,
        },
        isSaving ? "Saving..." : "Save",
      ),
    ];
  }

  return {
    oninit(vnode) {
      const attrs = vnode.attrs;
      latestAttrs = attrs;
      name = attrs.project.name;
      color = attrs.project.color;
      glyphIndex = normalizedGlyphIndex(attrs.project.glyph);
    },

    view(vnode) {
      const attrs = vnode.attrs;
      latestAttrs = attrs;
      const trimmedName = name.trim();

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
          title: "Project settings",
          actions: isConfirmingDelete ? deleteConfirmationActions(attrs) : editActions(attrs, trimmedName),
        },
        [
          // Live preview of the two pickers' combined result.
          m("div", { class: "mb-4 flex items-center gap-3" }, [
            m(
              "span",
              { class: "flex h-10 w-10 shrink-0 items-center justify-center" },
              m.trust(squiggleMarkup(glyphIndex, color, PREVIEW_GLYPH_SIZE)),
            ),
            m("span", { class: "text-primary truncate text-sm font-medium" }, trimmedName || "Untitled project"),
          ]),

          m("label", { class: MODAL_LABEL_CLASS }, "Name"),
          m("input", {
            class: inputClass({ extra: "mb-3" }),
            type: "text",
            value: name,
            placeholder: "project name",
            autofocus: true,
            disabled: isSaving || isDeleting,
            oninput(e: InputEvent) {
              name = (e.target as HTMLInputElement).value;
            },
            onkeydown(e: KeyboardEvent) {
              if (e.key === "Enter") {
                save(attrs);
              }
            },
          }),

          m("label", { class: MODAL_LABEL_CLASS }, "Color"),
          m("div", { class: "mb-3 flex flex-wrap gap-2" }, PALETTE.map(colorSwatch)),

          m("label", { class: MODAL_LABEL_CLASS }, "Squiggle"),
          m(
            "div",
            { class: "mb-3 grid grid-cols-5 gap-2" },
            SQUIGGLE_GLYPHS.map((_glyph, index) => glyphCell(index)),
          ),

          error ? m("p", { style: "color: red; font-size: 0.85em; margin-top: 4px;" }, error) : null,

          isConfirmingDelete ? deleteConfirmationMessage(attrs) : null,
        ],
      );
    },
  };
}
