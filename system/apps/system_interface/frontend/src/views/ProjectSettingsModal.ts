/**
 * Modal for one project's display metadata: its name, its color, which of the
 * ten squiggles stands for it, and its member list -- plus the one place a
 * project can be deleted.
 *
 * Reached from the sidebar's switcher header context menu, and only ever for a
 * real project. Creating no longer goes through here: the switcher's "New
 * project" mints "Project N" with the next unused glyph on the spot, so the
 * user lands in the New Tab launcher instead of on a form. Everything never
 * reaches here either -- it is a view rather than a project, with no name,
 * color, glyph or member list of its own and nothing to delete.
 *
 * This is the home for removing a member from a project -- the rail row's menu
 * no longer carries that verb, since it now renders the same per-kind verb set
 * the dock tab does, where every verb acts on the object rather than on one
 * view of it. So the list here has to be reachable and usable standalone
 * rather than depending on
 * anything that surface already resolved: it looks up each member's display
 * name and kind itself, through the same shared stores and helpers every other
 * naming surface reads (member-titles for a chosen name, the agent list for a
 * chat's, the derived-name helpers for a terminal's or browser's). Removing a
 * member here is exactly "remove from project" everywhere else: the ref drops
 * out of this project's member list and nothing else -- the object keeps
 * running and stays in Everything and in any other project already showing
 * it, which the row's own tooltip says plainly.
 *
 * Deleting is confirm-gated in place -- a second, red button inside this same
 * dialog rather than a second stacked dialog -- because the modal already owns
 * the screen and the name being deleted is right there in the preview.
 * Deleting a project is itself a pure view operation now: it removes the
 * project's own view and member list and nothing more, so there is no longer
 * any consequence for the confirmation to enumerate beyond that.
 *
 * Shell and class names follow the shared `.custom-url-dialog`
 * markup, a backdrop click to dismiss, Enter in the name field to save. Escape
 * is listened for on the document (as FastModeModal does) rather than on the
 * name field alone, because focus here just as easily sits on a swatch or a
 * glyph.
 */

import m from "mithril";
import { getAgentById, getProtoAgents } from "../models/AgentManager";
import { displayNameForMember } from "../models/MemberTitles";
import {
  browserSessionFromRef,
  chatAgentIdFromRef,
  deleteProjectRequest,
  memberKindFromRef,
  removeMember,
  serviceNameFromRef,
  terminalSessionFromRef,
  updateProjectSettings,
} from "../models/Projects";
import type { MemberKind, ProjectInfo } from "../models/Projects";
import { serviceIconMarkup } from "./appIcon";
import { browserDisplayName, chatDisplayName, terminalDisplayName } from "./derived-names";
import { hoverTooltipAttrs } from "./hoverTooltip";
import { icon } from "./icons";
import { SQUIGGLE_GLYPHS, squiggleMarkup } from "./squiggles";

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
  // Drop the ref's panel from the dock, for the case where the project being
  // edited is the one on screen. Removing a member has to undock its tab just
  // as the rail's old removal verb did, or the view keeps showing a panel for
  // an object it no longer lists.
  onMemberRemoved: (ref: string) => void;
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

// The size every member row's kind glyph and remove button draw at, matching
// the rail's own tab-list glyph and action-icon sizes respectively.
const MEMBER_GLYPH_SIZE = 16;
const MEMBER_ACTION_ICON_SIZE = 14;

// Inner markup for the four kind glyphs the rail's tab list and the New Tab
// launcher already draw (their own tables are private to those files, so this
// is a small, deliberate duplicate rather than a shared import); "url" is
// covered directly by icons.ts's own `external-link`, which those two surfaces
// also reach for.
const KIND_GLYPH_PATHS: Readonly<Record<Exclude<MemberKind, "url">, string>> = {
  chat:
    '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7' +
    'a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
  terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
  browser:
    '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/>' +
    '<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
  app: '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/>',
};

/** The built-in glyph for one member kind, on the same 24x24 stroke grid as
 *  every icon in icons.ts. */
function kindGlyphMarkup(kind: MemberKind): string {
  if (kind === "url") return icon("external-link", { size: MEMBER_GLYPH_SIZE });
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${MEMBER_GLYPH_SIZE}" height="${MEMBER_GLYPH_SIZE}" ` +
    `viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ` +
    `stroke-linejoin="round" aria-hidden="true">${KIND_GLYPH_PATHS[kind]}</svg>`
  );
}

/** The glyph one member row wears: an app's own registered icon when it has
 *  one, and the kind's built-in glyph otherwise -- the same fallback rule the
 *  rail and the launcher apply. */
function memberGlyphMarkup(ref: string, kind: MemberKind): string {
  const fallback = kindGlyphMarkup(kind);
  if (kind !== "app") return fallback;
  return serviceIconMarkup(serviceNameFromRef(ref), MEMBER_GLYPH_SIZE, fallback);
}

/**
 * What a member ref is called before factoring in a chosen name: a chat reads
 * its agent's live display name (falling back to a still-resolving proto
 * agent's, then the bare id), and everything else derives its display form
 * from the identity it is filed under, exactly as every other naming surface
 * in this workspace does.
 */
function derivedMemberLabel(ref: string, kind: MemberKind): string {
  switch (kind) {
    case "chat": {
      const agentId = chatAgentIdFromRef(ref) ?? ref;
      const agent = getAgentById(agentId);
      if (agent !== undefined) return chatDisplayName(agent);
      const proto = getProtoAgents().find((protoAgent) => protoAgent.agent_id === agentId);
      return proto !== undefined ? proto.name : agentId;
    }
    case "terminal":
      return terminalDisplayName(terminalSessionFromRef(ref) ?? ref);
    case "browser":
      return browserDisplayName(browserSessionFromRef(ref) ?? ref);
    case "app":
      return serviceNameFromRef(ref) ?? ref;
    case "url":
      // An ad-hoc page has no name of its own beyond its (currently unopened)
      // tab's title, which this modal has no way to reach.
      return "Page";
  }
}

/** The label one member row shows: its chosen name (member-titles) when it has
 *  one, else the derived one -- the same precedence every naming surface in
 *  the workspace follows. */
function memberLabel(ref: string, kind: MemberKind): string {
  return displayNameForMember(ref, derivedMemberLabel(ref, kind));
}

const KIND_NOUN: Readonly<Record<MemberKind, string>> = {
  chat: "Chat",
  terminal: "Terminal",
  browser: "Browser",
  app: "App",
  url: "Page",
};

export function ProjectSettingsModal(): m.Component<ProjectSettingsModalAttrs> {
  let name = "";
  let color = SQUIGGLE_GLYPHS[0].color;
  let glyphIndex = 0;
  let isSaving = false;
  let isDeleting = false;
  let isConfirmingDelete = false;
  let error: string | null = null;
  // A working copy of the project's member list, seeded from `attrs.project`
  // on open and updated locally as removals land -- the parent hands this
  // modal a snapshot rather than a live-updating prop (see the module
  // docstring), so this is the only place that stays in step with what this
  // dialog has actually done.
  let members: string[] = [];
  // The one member currently being removed, so its row shows its own pending
  // state and a second click on any row is a no-op rather than a race.
  let removingRef: string | null = null;
  let memberError: string | null = null;
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

  /** Drop one ref from this project's member list. This is "remove from
   *  project": the object keeps running and stays in Everything and in any
   *  other project already showing it -- nothing here stops it. */
  async function removeProjectMember(attrs: ProjectSettingsModalAttrs, ref: string): Promise<void> {
    if (removingRef !== null || isSaving || isDeleting) return;
    removingRef = ref;
    memberError = null;
    m.redraw();

    try {
      await removeMember(attrs.project.project_id, ref);
      members = members.filter((member) => member !== ref);
      attrs.onMemberRemoved(ref);
    } catch (e) {
      memberError = (e as Error).message ?? "Could not remove this member.";
    }
    removingRef = null;
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

  /** One member: its kind glyph, its display name, and the button that removes
   *  it from this project (and nothing else). */
  function memberRow(attrs: ProjectSettingsModalAttrs, ref: string): m.Vnode {
    const kind = memberKindFromRef(ref);
    const label = memberLabel(ref, kind);
    const isRowDisabled = isSaving || isDeleting || removingRef !== null;
    return m(
      "div",
      {
        key: ref,
        class: "project-settings-member group flex h-8 w-full items-center gap-2 px-1 text-text-primary",
      },
      [
        m(
          "span",
          { class: "flex shrink-0 items-center text-text-faint", title: KIND_NOUN[kind] },
          m.trust(memberGlyphMarkup(ref, kind)),
        ),
        m("span", { class: "min-w-0 flex-1 truncate text-sm" }, label),
        m(
          "button",
          {
            type: "button",
            class:
              "flex h-5 w-5 shrink-0 cursor-pointer items-center justify-center rounded text-text-faint " +
              "hover:text-text-primary hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-50",
            disabled: isRowDisabled,
            "aria-label": `Remove ${label} from this project`,
            ...hoverTooltipAttrs("Removes it from this project only. It keeps running and stays in Everything."),
            onclick: () => removeProjectMember(attrs, ref),
          },
          m.trust(icon("minus", { size: MEMBER_ACTION_ICON_SIZE })),
        ),
      ],
    );
  }

  /** The project's member list: every object this view shows, with its kind
   *  and a way to stop showing it here. Standing in for the rail row's own
   *  "Remove from project" verb (see the module docstring), so this has to
   *  work on its own rather than leaning on anything that surface resolved. */
  function membersSection(attrs: ProjectSettingsModalAttrs): m.Children {
    if (members.length === 0) {
      return m("p", { class: "mb-3 text-sm text-text-faint" }, "No members yet.");
    }
    return m(
      "div",
      { class: "mb-3 max-h-40 overflow-y-auto rounded-md border border-border" },
      members.map((ref) => memberRow(attrs, ref)),
    );
  }

  function deleteConfirmation(attrs: ProjectSettingsModalAttrs): m.Children {
    return [
      m("p.destroy-dialog-message", [
        "Delete ",
        m("strong", attrs.project.name),
        "? This removes the view only — everything it shows keeps running, and stays in Everything and " +
          "in any other project showing it.",
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
      members = [...attrs.project.members];
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

              m("label.custom-url-dialog-label", "Members"),
              membersSection(attrs),
              memberError
                ? m(
                    "p",
                    { style: "color: red; font-size: 0.85em; margin-top: -8px; margin-bottom: 8px;" },
                    memberError,
                  )
                : null,

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
