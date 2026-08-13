/**
 * Modal for one project's display metadata: its name, its color, and which of
 * the ten squiggles stands for it -- plus the one place a project can be
 * deleted.
 *
 * Reached from the sidebar's switcher header context menu, and only ever for a
 * real project. Creating no longer goes through here: the switcher's "New
 * project" mints "Project N" with the next unused glyph on the spot, so the
 * user lands in the New Tab launcher instead of on a form. Everything never
 * reaches here either -- it is a view rather than a project, with no name,
 * color or glyph of its own and nothing to delete.
 *
 * Deleting is confirm-gated in place -- a second, red button inside this same
 * dialog rather than a second stacked dialog -- because the modal already owns
 * the screen and the name being deleted is right there in the preview.
 *
 * Shell and class names follow the shared `.custom-url-dialog`
 * markup, a backdrop click to dismiss, Enter in the name field to save. Escape
 * is listened for on the document (as FastModeModal does) rather than on the
 * name field alone, because focus here just as easily sits on a swatch or a
 * glyph.
 */

import m from "mithril";
import { deleteProjectRequest, updateProjectSettings } from "../models/Projects";
import type { MemberKind, ProjectInfo } from "../models/Projects";
import { SQUIGGLE_GLYPHS, squiggleMarkup } from "./squiggles";
import { normalizeTabTitle, tabRenameBlockedReason } from "./tab-rename";

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
  // What this project currently shows, in the order the rail lists it. The
  // modal only renders and removes: the workspace owns membership, so dropping
  // a row calls back rather than writing to the store here.
  contents: readonly ProjectContentRow[];
  // Stop showing this object in this project, called once per staged row when
  // Save commits. The object keeps running and stays in every other project
  // holding it -- a project is a view, so this hides it here and nowhere else.
  // Awaited, so a rejection surfaces as the dialog's error rather than leaving
  // the list disagreeing with the store.
  onRemoveContent: (ref: string) => Promise<void>;
  // Name this object, called once per staged rename when Save commits. The name
  // belongs to the object rather than to the tab showing it, so it reaches
  // every view holding it -- and a row with no tab is renamed like any other.
  // Awaited like the removal above, so a refusal surfaces as the dialog's error
  // rather than leaving the list disagreeing with the machine.
  onRenameContent: (ref: string, title: string) => Promise<void>;
}

/** One row of the "What's in this project" list. Mirrors the rail's tab row,
 *  minus the parts only the rail needs. */
export interface ProjectContentRow {
  ref: string;
  kind: MemberKind;
  label: string;
  // Whether the object has a tab in the dock right now. A member with no panel
  // is backgrounded: still running, just not docked.
  isOpen: boolean;
}

// The palette is exactly the glyphs' own signature colors, so every project
// color belongs to the family the squiggles were drawn in.
const PALETTE: readonly string[] = SQUIGGLE_GLYPHS.map((glyph) => glyph.color);

const PREVIEW_GLYPH_SIZE = 40;
const PICKER_GLYPH_SIZE = 28;

// The rail draws each kind as a glyph, but its icon table is private to that
// module and importing it here would make the two circular (the rail already
// imports this modal). A settings list is prose-shaped anyway, so the kind
// reads as a word.
const KIND_LABEL: Record<MemberKind, string> = {
  chat: "Chat",
  browser: "Browser",
  terminal: "Terminal",
  app: "App",
  url: "Page",
};

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
  // Rows the user has marked for removal. Staged rather than applied on click
  // so this control obeys Save/Cancel like every other field in the dialog.
  let refsToRemove = new Set<string>();
  // New names, by ref, staged for the same reason and applied by the same Save.
  let titlesByRef = new Map<string, string>();
  // The row whose label is currently a text field, and what has been typed into
  // it. One at a time: this is a list, not a form.
  let editingRef: string | null = null;
  let editingTitle = "";
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
      // Renames first, then removals, then the metadata: the metadata save is
      // what closes the dialog, so doing it last means a failure anywhere
      // before it can still report itself here instead of vanishing behind the
      // dialog it just dismissed.
      for (const [ref, title] of titlesByRef) {
        await attrs.onRenameContent(ref, title);
      }
      titlesByRef = new Map<string, string>();
      for (const ref of refsToRemove) {
        await attrs.onRemoveContent(ref);
      }
      refsToRemove = new Set<string>();
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
        `outline: ${isSelected ? "2px solid var(--color-text-primary)" : "none"};`,
      title: swatch,
      "aria-label": `Color ${swatch}`,
      "aria-pressed": isSelected ? "true" : "false",
      onclick() {
        color = swatch;
      },
    });
  }

  /** What a row is called right now: the name staged for it, else the one the
   *  workspace derived. */
  function stagedLabel(row: ProjectContentRow): string {
    return titlesByRef.get(row.ref) ?? row.label;
  }

  /** Turn a row's label into a text field, seeded with what it says now. */
  function beginRowEdit(row: ProjectContentRow): void {
    editingRef = row.ref;
    editingTitle = stagedLabel(row);
  }

  /** Leave the editor, staging what was typed or throwing it away. A title that
   *  is empty once trimmed is not a name, so it stages nothing -- the same
   *  outcome as Escape. */
  function endRowEdit(row: ProjectContentRow, isCommitting: boolean): void {
    if (editingRef !== row.ref) return;
    editingRef = null;
    if (!isCommitting) return;
    const title = normalizeTabTitle(editingTitle);
    if (title === null) return;
    if (title === row.label) titlesByRef.delete(row.ref);
    else titlesByRef.set(row.ref, title);
  }

  /** One row's label: a name, or the field it becomes while being renamed. */
  function rowLabel(row: ProjectContentRow, isStaged: boolean): m.Vnode {
    const isRenamed = titlesByRef.has(row.ref);
    const labelClass =
      "min-w-0 flex-1 truncate text-[13px] " +
      (isStaged
        ? "text-text-faint line-through"
        : isRenamed
          ? "text-text-primary italic"
          : row.isOpen
            ? "text-text-primary"
            : "text-text-faint");

    if (editingRef === row.ref) {
      return m("input", {
        class: "border-accent bg-bg text-text-primary min-w-0 flex-1 rounded border px-1 text-[13px] outline-none",
        type: "text",
        value: editingTitle,
        disabled: isSaving || isDeleting,
        oncreate(vnode: m.VnodeDOM) {
          const field = vnode.dom as HTMLInputElement;
          field.focus();
          field.select();
        },
        oninput(e: InputEvent) {
          editingTitle = (e.target as HTMLInputElement).value;
        },
        onkeydown(e: KeyboardEvent) {
          if (e.key !== "Enter" && e.key !== "Escape") return;
          // Escape backs out of the edit rather than out of the dialog, so it
          // must not reach the document-level handler that closes this.
          e.stopPropagation();
          e.preventDefault();
          endRowEdit(row, e.key === "Enter");
        },
        onblur() {
          endRowEdit(row, true);
        },
      });
    }

    // A struck-through row is on its way out of this list, so it is not offered
    // a name in the same visit. A backgrounded row is renameable like any
    // other: the name belongs to the object, not to a tab it may not have.
    const blockedReason = tabRenameBlockedReason({ isStagedForRemoval: isStaged });
    return m(
      "span",
      {
        class: labelClass,
        title:
          blockedReason ??
          (isRenamed
            ? `Renamed from "${row.label}" on save. Double-click to change it again.`
            : "Double-click to rename this. The name is the object's, so every project showing it says the same."),
        ondblclick:
          blockedReason === null
            ? () => {
                beginRowEdit(row);
              }
            : undefined,
      },
      stagedLabel(row),
    );
  }

  /** What the project currently shows, with a way to rename or drop each row.
   *
   *  Removing hides the object in this project only -- it keeps running and
   *  stays in every other project holding it, and in Everything -- so the
   *  button says "Remove" rather than anything that sounds like stopping or
   *  deleting it. Rows the dock is not currently showing are the backgrounded
   *  ones, and read as tertiary the same way the rail draws them.
   *
   *  Removals are STAGED, not applied on click: a row marked for removal is
   *  struck through until Save commits it, and Cancel throws the marks away
   *  with the rest of the form. Applying immediately would have made this the
   *  one control in the dialog that ignored Save -- the user would rename, hit
   *  Cancel expecting to have changed nothing, and find their tabs gone.
   *
   *  Renames stage the same way and for the same reason. Double-clicking a
   *  label turns it into a field; the new name shows in italic until Save
   *  applies it, and Cancel throws it away with everything else. The tab strip's
   *  own double-click rename commits on the spot, which is not a disagreement:
   *  there is no Save button on a tab strip, so Enter/Escape is the whole
   *  contract there, while everything inside a dialog with a Save button obeys
   *  it. Every row can be renamed, backgrounded ones included -- the name is
   *  filed against the object, not against a tab -- except one already staged
   *  for removal, which is on its way out of this list (see
   *  ``tabRenameBlockedReason``, whose sentence is that row's tooltip). */
  function contentsList(attrs: ProjectSettingsModalAttrs): m.Vnode {
    if (attrs.contents.length === 0) {
      return m(
        "p",
        { class: "text-text-faint mb-3 text-[13px]" },
        "Nothing yet. Open a chat, browser, terminal or app while this project is showing and it lands here.",
      );
    }
    const removeCount = attrs.contents.filter((row) => refsToRemove.has(row.ref)).length;
    const renameCount = attrs.contents.filter((row) => titlesByRef.has(row.ref)).length;
    return m("div", { class: "mb-3" }, [
      // The count names what the box is, and the fixed max height plus a scroll
      // region is what makes it read as a list you can page through rather than
      // as a few stray rows. What is staged is counted beside it, so a dialog
      // left open a while still says what pressing Save would do.
      m("div", { class: "text-text-faint mb-1 flex items-baseline justify-between text-[11px]" }, [
        m("span", `${attrs.contents.length} ${attrs.contents.length === 1 ? "item" : "items"}`),
        m("span", { class: "flex gap-2" }, [
          renameCount === 0 ? null : m("span", `${renameCount} to rename on save`),
          removeCount === 0 ? null : m("span", { class: "text-red-600" }, `${removeCount} to remove on save`),
        ]),
      ]),
      m(
        "div",
        { class: "border-border bg-bg max-h-48 overflow-y-auto rounded-md border" },
        attrs.contents.map((row) => {
          const isStaged = refsToRemove.has(row.ref);
          return m(
            "div",
            {
              key: row.ref,
              class:
                "project-contents-row border-border/60 group flex h-8 items-center gap-2 border-b px-2 last:border-b-0 " +
                (isStaged ? "bg-red-50" : "hover:bg-bg-hover"),
            },
            [
              m("span", { class: "text-text-faint w-16 shrink-0 text-[11px]" }, KIND_LABEL[row.kind]),
              rowLabel(row, isStaged),
              m(
                "button",
                {
                  type: "button",
                  // Shown on hover so the list stays quiet at rest, but always
                  // shown on a staged row -- that row's only way back is this
                  // button, so it must not be hidden behind a hover.
                  class:
                    "shrink-0 cursor-pointer rounded px-1.5 py-0.5 text-[12px] bg-transparent disabled:opacity-50 " +
                    (isStaged
                      ? "text-text-secondary hover:bg-bg-active opacity-100"
                      : "text-text-faint opacity-0 group-hover:opacity-100 hover:bg-red-100 hover:text-red-700"),
                  disabled: isSaving || isDeleting,
                  title: isStaged
                    ? "Keep this in the project after all"
                    : "Remove from this project on save. It keeps running and stays in Everything.",
                  onclick: () => {
                    if (isStaged) {
                      refsToRemove.delete(row.ref);
                      return;
                    }
                    refsToRemove.add(row.ref);
                    // Nothing left to name: the row is leaving the project, and
                    // a rename staged behind a removal would apply to a tab
                    // that is about to be closed.
                    titlesByRef.delete(row.ref);
                    if (editingRef === row.ref) editingRef = null;
                  },
                },
                isStaged ? "Undo" : "Remove",
              ),
            ],
          );
        }),
      ),
    ]);
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

  function deleteConfirmation(attrs: ProjectSettingsModalAttrs): m.Children {
    return [
      m("p.destroy-dialog-message", [
        "Delete ",
        m("strong", attrs.project.name),
        "? This deletes the view, not what it showed: every chat, terminal, browser and app in it keeps " +
          "running, stays in Everything, and stays in any other project showing it.",
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
      name = attrs.project.name;
      color = attrs.project.color;
      glyphIndex = normalizedGlyphIndex(attrs.project.glyph);
      refsToRemove = new Set<string>();
      titlesByRef = new Map<string, string>();
      editingRef = null;
      editingTitle = "";
    },

    view(vnode) {
      const attrs = vnode.attrs;
      latestAttrs = attrs;
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
              m("h3.custom-url-dialog-title", "Project settings"),

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

              m("label.custom-url-dialog-label", "In this project"),
              contentsList(attrs),

              error ? m("p", { style: "color: red; font-size: 0.85em; margin-top: 4px;" }, error) : null,

              isConfirmingDelete
                ? deleteConfirmation(attrs)
                : m("div.custom-url-dialog-actions", [
                    m(
                      "button.destroy-dialog-btn.destroy-dialog-btn-cancel",
                      {
                        class: "mr-auto disabled:opacity-50",
                        disabled: isSaving || isDeleting,
                        onclick() {
                          isConfirmingDelete = true;
                        },
                      },
                      "Delete",
                    ),
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
                      isSaving ? "Saving..." : "Save",
                    ),
                  ]),
            ],
          ),
        ],
      );
    },
  };
}
