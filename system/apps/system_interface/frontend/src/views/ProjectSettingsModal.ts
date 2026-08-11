/**
 * Modal for one project's display metadata: its name, its color, and which of
 * the ten squiggles stands for it.
 *
 * One component serves both "edit an existing project" and "create a new one".
 * The two differ only in which Projects API call Save makes and in whether a
 * Delete affordance exists at all -- create mode has nothing to delete yet, and
 * the Everything project can never be deleted, so its Delete button is
 * permanently disabled rather than hidden (a missing button reads as a bug; a
 * disabled one with a tooltip explains itself).
 *
 * Deleting is confirm-gated in place -- a second, red button inside this same
 * dialog rather than a second stacked dialog -- because the modal already owns
 * the screen and the name being deleted is right there in the preview.
 *
 * Shell and class names follow LayoutDialog -- the shared `.custom-url-dialog`
 * markup, a backdrop click to dismiss, Enter in the name field to save. Escape
 * is listened for on the document (as FastModeModal does) rather than on the
 * name field alone, because focus here just as easily sits on a swatch or a
 * glyph.
 */

import m from "mithril";
import { EVERYTHING_PROJECT_ID, createProject, deleteProjectRequest, updateProjectSettings } from "../models/Projects";
import type { ProjectInfo } from "../models/Projects";
import { SQUIGGLE_GLYPHS, squiggleMarkup } from "./squiggles";

interface ProjectSettingsCommonAttrs {
  // Fired with the server's copy of the project once the save lands, so the
  // parent can close the modal and adopt the stored values (the server
  // normalizes the name, and a create also mints the id).
  onSaved: (project: ProjectInfo) => void;
  onCancel: () => void;
}

export interface ProjectSettingsCreateAttrs extends ProjectSettingsCommonAttrs {
  mode: "create";
}

export interface ProjectSettingsEditAttrs extends ProjectSettingsCommonAttrs {
  // Optional, because attrs that carry a project are an edit by construction.
  // Only a create has to say so, having no project to go on.
  mode?: "edit";
  project: ProjectInfo;
  // Fired once the project is gone server-side. The parent re-lists and, if
  // this was the active project, switches away from it.
  onDeleted: (projectId: string) => void;
}

// A create has no project to carry and no delete callback to honour, so the
// two modes are separate shapes rather than one shape with optional fields.
export type ProjectSettingsModalAttrs = ProjectSettingsCreateAttrs | ProjectSettingsEditAttrs;

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
      const saved =
        attrs.mode === "create"
          ? await createProject(chosen, color, glyphIndex)
          : await updateProjectSettings(attrs.project.project_id, chosen, color, glyphIndex);
      attrs.onSaved(saved);
    } catch (e) {
      error = (e as Error).message ?? "The project could not be saved.";
      isSaving = false;
    }
    m.redraw();
  }

  async function deleteProject(attrs: ProjectSettingsEditAttrs): Promise<void> {
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
        `outline: ${isSelected ? "2px solid var(--color-text-primary)" : "none"};`,
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
          `border-color: ${isSelected ? color : "var(--color-border)"};` +
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

  function deleteButton(attrs: ProjectSettingsEditAttrs): m.Vnode {
    const isEverything = attrs.project.project_id === EVERYTHING_PROJECT_ID;
    const isDisabled = isEverything || isSaving || isDeleting;
    // The tooltip rides on a wrapper rather than on the button, because browsers
    // swallow pointer events on a disabled control -- and the disabled case is
    // exactly the one that needs explaining. The cursor is inline for the same
    // kind of reason: `.destroy-dialog-btn` sets `cursor: pointer` outside any
    // cascade layer, so a Tailwind `disabled:` utility would lose to it.
    return m(
      "span",
      {
        class: "mr-auto flex",
        title: isEverything ? "Everything holds every tab, so it can't be deleted" : "Delete this project",
      },
      m(
        "button.destroy-dialog-btn.destroy-dialog-btn-cancel",
        {
          class: "disabled:opacity-50",
          style: isDisabled ? "cursor: not-allowed;" : "",
          disabled: isDisabled,
          onclick() {
            isConfirmingDelete = true;
          },
        },
        "Delete",
      ),
    );
  }

  function deleteConfirmation(attrs: ProjectSettingsEditAttrs): m.Children {
    return [
      m("p.destroy-dialog-message", [
        "Delete ",
        m("strong", attrs.project.name),
        "? Its tabs stay open in Everything — nothing is destroyed and every transcript stays accessible.",
      ]),
      m("div.custom-url-dialog-actions", [
        m(
          "button.custom-url-dialog-cancel",
          {
            disabled: isDeleting,
            onclick() {
              isConfirmingDelete = false;
            },
          },
          "Keep project",
        ),
        m(
          "button.destroy-dialog-btn.destroy-dialog-btn-destroy",
          {
            class: "disabled:opacity-50",
            disabled: isDeleting,
            onclick: () => deleteProject(attrs),
          },
          isDeleting ? "Deleting..." : "Delete project",
        ),
      ]),
    ];
  }

  return {
    oninit(vnode) {
      const attrs = vnode.attrs;
      latestAttrs = attrs;
      if (attrs.mode === "create") {
        // A fresh project opens on a random squiggle in that glyph's own color,
        // so projects made without touching the pickers still look distinct.
        glyphIndex = Math.floor(Math.random() * SQUIGGLE_GLYPHS.length);
        color = SQUIGGLE_GLYPHS[glyphIndex].color;
      } else {
        name = attrs.project.name;
        color = attrs.project.color;
        glyphIndex = normalizedGlyphIndex(attrs.project.glyph);
      }
    },

    view(vnode) {
      const attrs = vnode.attrs;
      latestAttrs = attrs;
      const isCreate = attrs.mode === "create";
      // Narrowed once, so the delete affordances below take a shape that is
      // guaranteed to carry a project and an onDeleted.
      const editAttrs: ProjectSettingsEditAttrs | null = attrs.mode === "create" ? null : attrs;
      const trimmedName = name.trim();

      return m(
        "div.custom-url-dialog-overlay",
        {
          oncreate() {
            document.addEventListener("keydown", handleKeydown);
          },
          onremove() {
            document.removeEventListener("keydown", handleKeydown);
          },
          onclick(e: MouseEvent) {
            if ((e.target as HTMLElement).classList.contains("custom-url-dialog-overlay")) {
              attrs.onCancel();
            }
          },
        },
        [
          m(
            "div.custom-url-dialog",
            {
              onclick(e: MouseEvent) {
                e.stopPropagation();
              },
            },
            [
              m("h3.custom-url-dialog-title", isCreate ? "New project" : "Project settings"),

              // Live preview of the two pickers' combined result.
              m("div", { class: "mb-4 flex items-center gap-3" }, [
                m(
                  "span",
                  { class: "flex h-10 w-10 shrink-0 items-center justify-center" },
                  m.trust(squiggleMarkup(glyphIndex, color, PREVIEW_GLYPH_SIZE)),
                ),
                m(
                  "span",
                  { class: "text-text-primary truncate text-sm font-medium" },
                  trimmedName || "Untitled project",
                ),
              ]),

              m("label.custom-url-dialog-label", "Name"),
              m("input.custom-url-dialog-input", {
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

              m("label.custom-url-dialog-label", "Color"),
              m("div", { class: "mb-3 flex flex-wrap gap-2" }, PALETTE.map(colorSwatch)),

              m("label.custom-url-dialog-label", "Squiggle"),
              m(
                "div",
                { class: "mb-3 grid grid-cols-5 gap-2" },
                SQUIGGLE_GLYPHS.map((_glyph, index) => glyphCell(index)),
              ),

              error ? m("p", { style: "color: red; font-size: 0.85em; margin-top: 4px;" }, error) : null,

              isConfirmingDelete && editAttrs !== null
                ? deleteConfirmation(editAttrs)
                : m("div.custom-url-dialog-actions", [
                    editAttrs === null ? null : deleteButton(editAttrs),
                    m(
                      "button.custom-url-dialog-cancel",
                      {
                        onclick: attrs.onCancel,
                        disabled: isSaving || isDeleting,
                      },
                      "Cancel",
                    ),
                    m(
                      "button.custom-url-dialog-open",
                      {
                        onclick: () => save(attrs),
                        disabled: isSaving || isDeleting || !trimmedName,
                      },
                      isSaving ? (isCreate ? "Creating..." : "Saving...") : isCreate ? "Create" : "Save",
                    ),
                  ]),
            ],
          ),
        ],
      );
    },
  };
}
