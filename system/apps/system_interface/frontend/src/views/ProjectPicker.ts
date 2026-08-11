/**
 * The top-left project switcher.
 *
 * The trigger is the active project's squiggle, its name, and a chevron;
 * clicking opens a menu of every project on the machine (each with its own
 * squiggle and the active one checked), plus "New project..." and a per-row
 * settings affordance.
 *
 * Switching is deliberately split in two. The picker owns the *choice* -- it
 * persists the per-browser active id through ClientIdentity, exactly as the
 * layout picker does -- while loading that project's saved dockview state is
 * the workspace's job, reached through the optional `onSelectProject` attr.
 * That keeps this component free of any dockview knowledge, and means the
 * choice still survives a reload even before the workspace has wired itself up.
 *
 * Every mutation (create, rename/restyle, delete) belongs to
 * ProjectSettingsModal, which the picker opens in "create" mode for
 * "New project..." and in "edit" mode from a row's settings affordance. The
 * whole of the coupling between the two components is that modal's exported
 * attrs union: the picker hands it a mode (plus the project, when editing) and
 * re-lists on the way back out rather than tracking what changed.
 */

import m from "mithril";
import { getActiveProjectId, getStoredProjectId, setActiveProjectId } from "../models/ClientIdentity";
import { chooseInitialProject, fetchProjectsList, type ProjectInfo } from "../models/Projects";
import { icon } from "./icons";
import { ProjectSettingsModal, type ProjectSettingsModalAttrs } from "./ProjectSettingsModal";
import { squiggleMarkup } from "./squiggles";

export interface ProjectPickerAttrs {
  // Called with the project the user picked, after the choice has been
  // persisted. The workspace loads that project's content in response; when it
  // is absent the picker still records the choice for the next reload.
  onSelectProject?: (projectId: string) => void;
}

const TRIGGER_SQUIGGLE_SIZE = 18;
const MENU_SQUIGGLE_SIZE = 16;

const MENU_ROW_CLASS = "flex h-8 w-full cursor-pointer items-center gap-2 px-3 text-left hover:bg-bg-hover";

export function ProjectPicker(): m.Component<ProjectPickerAttrs> {
  let projects: ProjectInfo[] = [];
  let isMenuOpen = false;
  // What the settings modal is open on: "create" for a project that does not
  // exist yet, the project being edited otherwise, null while it is closed.
  let settingsTarget: "create" | ProjectInfo | null = null;
  let rootElement: HTMLElement | null = null;

  // Stable references (defined once for the component's life) so the menu's
  // add/removeEventListener pair to the same functions -- a per-render closure
  // would leak a listener each time the menu reopens.
  function handleOutsideMousedown(event: MouseEvent): void {
    if (rootElement !== null && !rootElement.contains(event.target as Node)) {
      isMenuOpen = false;
      m.redraw();
    }
  }

  function handleMenuKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape") {
      isMenuOpen = false;
      m.redraw();
    }
  }

  /** Persist the picked project and hand the actual switch to the workspace. */
  function selectProject(projectId: string, attrs: ProjectPickerAttrs): void {
    setActiveProjectId(projectId);
    attrs.onSelectProject?.(projectId);
  }

  /** Re-read the registry. Also the recovery path: when the project this
   *  client is on has disappeared (deleted from the settings modal, or by
   *  another client), the picker moves it to the same fallback a fresh client
   *  would land on. */
  async function refreshProjects(attrs: ProjectPickerAttrs): Promise<void> {
    projects = (await fetchProjectsList()).projects;
    // The workspace owns the *initial* pick -- getActiveProjectId() is empty
    // until it commits one -- so an empty id here is startup, not a gap.
    const activeId = getActiveProjectId();
    if (activeId !== "" && !projects.some((project) => project.project_id === activeId)) {
      const fallback = chooseInitialProject(projects, activeId);
      if (fallback !== null) {
        selectProject(fallback.project_id, attrs);
      }
    }
    m.redraw();
  }

  function openMenu(attrs: ProjectPickerAttrs): void {
    isMenuOpen = true;
    // Refreshed on open, like the "+" menu's browser fleet, so a project
    // another client just created or renamed is in the list.
    void refreshProjects(attrs);
  }

  function renderProjectRow(project: ProjectInfo, isActive: boolean, attrs: ProjectPickerAttrs): m.Vnode {
    return m(
      "div",
      {
        key: project.project_id,
        class: `${MENU_ROW_CLASS} ${isActive ? "font-semibold text-text-primary" : "text-text-secondary"}`,
        role: "menuitem",
        onclick: () => {
          isMenuOpen = false;
          selectProject(project.project_id, attrs);
        },
      },
      [
        m(
          "span",
          { class: "flex shrink-0 items-center" },
          m.trust(squiggleMarkup(project.glyph, project.color, MENU_SQUIGGLE_SIZE)),
        ),
        m("span", { class: "min-w-0 flex-1 truncate" }, project.name),
        isActive
          ? m(
              "span",
              { class: "flex shrink-0 items-center text-text-secondary" },
              m.trust(icon("check", { size: 14 })),
            )
          : null,
        m(
          "button",
          {
            type: "button",
            class:
              "project-picker-settings shrink-0 cursor-pointer rounded px-1 leading-none text-text-faint " +
              "hover:text-text-primary",
            title: `Settings for ${project.name}`,
            "aria-label": `Settings for ${project.name}`,
            onclick: (event: MouseEvent) => {
              // The row underneath switches projects; opening settings must not.
              event.stopPropagation();
              isMenuOpen = false;
              settingsTarget = project;
            },
          },
          "···",
        ),
      ],
    );
  }

  function renderMenu(activeId: string, attrs: ProjectPickerAttrs): m.Vnode {
    return m(
      "div",
      {
        class:
          "project-picker-menu absolute top-full left-0 z-50 mt-1 min-w-[240px] rounded-base border " +
          "border-border bg-surface py-1 shadow-lg",
        role: "menu",
        // Escape and any click outside the picker close the menu, matching the
        // composer's model dropdown.
        oncreate: () => {
          document.addEventListener("mousedown", handleOutsideMousedown);
          document.addEventListener("keydown", handleMenuKeydown);
        },
        onremove: () => {
          document.removeEventListener("mousedown", handleOutsideMousedown);
          document.removeEventListener("keydown", handleMenuKeydown);
        },
      },
      [
        projects.map((project) => renderProjectRow(project, project.project_id === activeId, attrs)),
        m("div", { class: "my-1 border-t border-border" }),
        m(
          "div",
          {
            class: `${MENU_ROW_CLASS} text-text-secondary`,
            role: "menuitem",
            onclick: () => {
              isMenuOpen = false;
              settingsTarget = "create";
            },
          },
          [
            m("span", { class: "flex w-4 shrink-0 items-center justify-center" }, "+"),
            m("span", { class: "min-w-0 flex-1 truncate" }, "New project..."),
          ],
        ),
      ],
    );
  }

  /** The settings modal, in whichever mode the last click asked for. Both
   *  successful outcomes re-list rather than patching the cached registry: the
   *  server normalizes names and mints ids, and a delete has to be reconciled
   *  against the active project anyway. */
  function renderSettingsModal(attrs: ProjectPickerAttrs): m.Children {
    const target = settingsTarget;
    if (target === null) return null;

    const close = (): void => {
      settingsTarget = null;
    };

    const modalAttrs: ProjectSettingsModalAttrs =
      target === "create"
        ? {
            mode: "create",
            onSaved: (project: ProjectInfo) => {
              close();
              // A project is created to be worked in, so creating switches.
              selectProject(project.project_id, attrs);
              void refreshProjects(attrs);
            },
            onCancel: close,
          }
        : {
            mode: "edit",
            project: target,
            onSaved: () => {
              close();
              void refreshProjects(attrs);
            },
            onDeleted: () => {
              close();
              void refreshProjects(attrs);
            },
            onCancel: close,
          };
    return m(ProjectSettingsModal, modalAttrs);
  }

  return {
    oninit(vnode) {
      void refreshProjects(vnode.attrs);
    },

    view(vnode) {
      const attrs = vnode.attrs;
      // Before the workspace commits a choice, the trigger previews the project
      // this client will land on -- the one the workspace itself resolves from
      // the same stored id.
      const active =
        projects.find((project) => project.project_id === getActiveProjectId()) ??
        chooseInitialProject(projects, getStoredProjectId());

      return m(
        "div",
        {
          class: "project-picker relative",
          oncreate: (pickerVnode: m.VnodeDOM) => {
            rootElement = pickerVnode.dom as HTMLElement;
          },
          onremove: () => {
            rootElement = null;
          },
        },
        [
          m(
            "button",
            {
              type: "button",
              class:
                "project-picker-trigger flex h-8 max-w-56 cursor-pointer items-center gap-2 rounded-base px-2 " +
                "text-text-primary hover:bg-bg-hover",
              title: active === null ? "Projects" : `Project: ${active.name}`,
              "aria-haspopup": "menu",
              "aria-expanded": isMenuOpen ? "true" : "false",
              onclick: (event: MouseEvent) => {
                event.stopPropagation();
                if (isMenuOpen) {
                  isMenuOpen = false;
                } else {
                  openMenu(attrs);
                }
              },
            },
            [
              active === null
                ? null
                : m(
                    "span",
                    { class: "flex shrink-0 items-center" },
                    m.trust(squiggleMarkup(active.glyph, active.color, TRIGGER_SQUIGGLE_SIZE)),
                  ),
              m(
                "span",
                { class: "min-w-0 flex-1 truncate font-semibold" },
                active === null ? "Projects" : active.name,
              ),
              m(
                "span",
                { class: "flex shrink-0 items-center text-text-secondary" },
                m.trust(icon("chevron-down", { size: 14 })),
              ),
            ],
          ),
          isMenuOpen ? renderMenu(active === null ? "" : active.project_id, attrs) : null,
          renderSettingsModal(attrs),
        ],
      );
    },
  };
}
