/**
 * The App Store page, built like Getting Started and Routines: a heading and a line on what it is, a strip of what
 * the workspace has free, a switch between the two sections, and a search field.
 *
 * "Apps" lists the apps this workspace has (its built-in apps, the ones made here, and the ones the store
 * installed), then the store's apps still to install, as tiles. "Templates" is Getting Started's shelves of the
 * published catalog. Picking a store app or a template shows its detail page inside this one, with a way back.
 * Opening an app and adopting a template go through the shell (``openLink`` and ``startWithText``).
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { SegmentedControl } from "@imbue/workspace-ui/src/components/segmentedControl";
import { matchesQuery } from "@imbue/workspace-ui/src/search";
import type { CatalogTemplate, TemplateCatalogState } from "getting-started-frontend/src/models/TemplateCatalog";
import { resolveShelves, searchTemplates } from "getting-started-frontend/src/models/TemplateCatalog";
import { TemplateCard, TemplateShelves } from "getting-started-frontend/src/views/TemplateShelves";
import type { StoreApp, StoreSnapshot, StoreState, WorkspaceApp } from "../models/Store";
import {
  addApp,
  availableApps,
  cancelFindCommand,
  checkAddedApp,
  confirmRepoAdd,
  discardRepoAdd,
  fetchFinderJob,
  finderLogUrl,
  finderScreenshotUrl,
  fetchTemplateSizeKb,
  isInCategory,
  removeAddedApp,
  requestInstall,
  requestUninstall,
  requestUpdate,
  startFindCommand,
  storeAppFor,
  titleOf,
  uninstallingApps,
} from "../models/Store";
import { formatMb } from "../room";
import { AppDetail } from "./AppDetail";
import type { AppActions } from "./AppTile";
import { StoreAppTile, WorkspaceAppTile } from "./AppTile";
import { AddAppDialog } from "./AddAppDialog";
import { InstallDialog, RemoveDialog, RepoAddDialog, UninstallDialog, UpdateDialog } from "./dialogs";
import type { TemplateSize } from "./TemplateDetail";
import { StoreTemplateDetail } from "./TemplateDetail";

type Section = "apps" | "templates";
/** The dialogs that ask about one store app. */
type AppDialogKind = "install" | "uninstall" | "remove" | "update";

const SECTION_HEADING_CLASS = "type-section text-faint";
const FIELD_GLYPH_SIZE = 14;
const GRID_CLASS = "grid grid-cols-2 gap-3 @max-[560px]:grid-cols-1";
const WORKSPACE_GRID_CLASS = "grid grid-cols-3 gap-2 @max-[760px]:grid-cols-2 @max-[480px]:grid-cols-1";

export interface StorePageAttrs {
  store: StoreState;
  templates: TemplateCatalogState;
  onOpenLink: (url: string) => void;
  onStartWithText: (text: string) => void;
}

function stat(value: string, label: string, hook: string): m.Vnode {
  return m("div", { class: "flex flex-col gap-0.5", "data-stat": hook }, [
    m("span", { class: "type-heading whitespace-nowrap text-primary" }, value),
    m("span", { class: "type-tag uppercase tracking-wide text-faint" }, label),
  ]);
}

function matchesStoreApp(app: StoreApp, query: string): boolean {
  return matchesQuery(query, app.title, app.description, app.author);
}

function matchesWorkspaceApp(app: WorkspaceApp, query: string): boolean {
  return matchesQuery(query, app.display_name, app.description);
}

export function StorePage(): m.Component<StorePageAttrs> {
  let section: Section = "apps";
  let query = "";
  /** The chosen category's key; null is "All apps". */
  let category: string | null = null;
  let detailSlug: string | null = null;
  let detailTemplate: CatalogTemplate | null = null;
  let templateSize: TemplateSize = { kind: "loading" };
  let dialog: { kind: AppDialogKind; slug: string; version?: string } | null = null;
  let isAdding = false;
  let dialogError: string | null = null;
  let isSubmitting = false;
  // Apps from GitHub not to ask about again in this visit: put off with "Not now" (they stay waiting in the store), or
  // just confirmed or discarded (the next poll drops them; until then their buttons must not be pressed twice).
  const settledRepoAdds = new Set<string>();
  // Why the store refused the last answer about a pending add, and which one it was about.
  let repoAddError: { id: string; reason: string } | null = null;
  let isSubmittingRepoAdd = false;

  function openDialog(kind: AppDialogKind, app: StoreApp, version?: string): void {
    dialog = { kind, slug: app.slug, version };
    dialogError = null;
    isSubmitting = false;
  }

  function closeDialog(): void {
    dialog = null;
    dialogError = null;
    isSubmitting = false;
  }

  async function submit(job: () => Promise<string | null>): Promise<void> {
    isSubmitting = true;
    const refusal = await job();
    isSubmitting = false;
    if (refusal === null) closeDialog();
    else dialogError = refusal;
    m.redraw();
  }

  function pickTemplate(template: CatalogTemplate): void {
    detailTemplate = template;
    templateSize = { kind: "loading" };
    void fetchTemplateSizeKb(template.slug).then((sizeKb) => {
      if (detailTemplate?.slug !== template.slug) return;
      templateSize = sizeKb === null ? { kind: "unknown" } : { kind: "known", sizeKb };
      m.redraw();
    });
  }

  function actions(attrs: StorePageAttrs): AppActions {
    return {
      onPick: (app) => (detailSlug = app.slug),
      onInstall: (app) => openDialog("install", app),
      onOpen: (url) => attrs.onOpenLink(url),
      onUninstall: (app) => openDialog("uninstall", app),
    };
  }

  function appsSection(snapshot: StoreSnapshot, attrs: StorePageAttrs): m.Children {
    const trimmed = query.trim();
    const isShown = (app: StoreApp): boolean =>
      isInCategory(app, category) && (trimmed === "" || matchesStoreApp(app, trimmed));
    // A chosen category narrows this workspace's apps to the store apps in it: the built-in ones carry no tags.
    const workspaceApps = snapshot.workspace_apps.filter((app) => {
      const storeApp = storeAppFor(snapshot, app);
      if (category !== null && (storeApp === null || !isInCategory(storeApp, category))) return false;
      return trimmed === "" || matchesWorkspaceApp(app, trimmed);
    });
    const leaving = uninstallingApps(snapshot).filter(isShown);
    const available = availableApps(snapshot).filter(isShown);
    if (workspaceApps.length + leaving.length + available.length === 0) {
      const categoryTitle = snapshot.categories.find((candidate) => candidate.key === category)?.title;
      const message =
        trimmed !== ""
          ? `No apps match “${trimmed}”${categoryTitle === undefined ? "" : ` in ${categoryTitle}`}.`
          : `No apps in ${categoryTitle ?? "the store"} yet.`;
      return m("p", { class: "mt-6 type-body text-secondary", "data-no-matches": "" }, message);
    }
    const busyTitle = titleOf(snapshot, snapshot.busy_with);
    const handlers = actions(attrs);
    return [
      workspaceApps.length + leaving.length === 0
        ? null
        : m("section", { class: "mt-6", "data-section": "installed" }, [
            m("h2", { class: `${SECTION_HEADING_CLASS} mb-2` }, "On this workspace"),
            m("div", { class: WORKSPACE_GRID_CLASS }, [
              ...workspaceApps.map((app) =>
                m(WorkspaceAppTile, { key: `w-${app.name}`, app, storeApp: storeAppFor(snapshot, app), ...handlers }),
              ),
              ...leaving.map((app) =>
                m(StoreAppTile, { key: `s-${app.slug}`, app, busyWithTitle: null, ...handlers }),
              ),
            ]),
          ]),
      available.length === 0
        ? null
        : m("section", { class: "mt-8", "data-section": "available" }, [
            m("h2", { class: `${SECTION_HEADING_CLASS} mb-2` }, "Available to install"),
            m(
              "div",
              { class: GRID_CLASS },
              available.map((app) =>
                m(StoreAppTile, {
                  key: `s-${app.slug}`,
                  app,
                  busyWithTitle: snapshot.busy_with !== null && snapshot.busy_with !== app.slug ? busyTitle : null,
                  ...handlers,
                }),
              ),
            ),
          ]),
    ];
  }

  function templatesSection(templates: TemplateCatalogState): m.Children {
    switch (templates.kind) {
      case "loading":
        return m("p", { class: "mt-6 type-body text-faint" }, "Loading templates…");
      case "failed":
        return m("p", { class: "mt-6 type-body text-faint" }, "Failed to load templates.");
      case "disabled":
        return m("p", { class: "mt-6 type-body text-faint" }, "No template catalog is set up for this workspace.");
      case "loaded": {
        const trimmed = query.trim();
        if (trimmed === "") {
          return m(
            "div",
            { class: "mt-4" },
            m(TemplateShelves, { shelves: resolveShelves(templates.catalog), onPick: pickTemplate }),
          );
        }
        const matches = searchTemplates(templates.catalog.templates, trimmed);
        if (matches.length === 0) {
          return m(
            "p",
            { class: "mt-6 type-body text-secondary", "data-no-matches": "" },
            `No templates match “${trimmed}”.`,
          );
        }
        return m(
          "div",
          { class: "mt-6 grid grid-cols-4 gap-6 @max-[620px]:grid-cols-2" },
          matches.map((template) =>
            m(TemplateCard, { key: template.slug, template, isFill: true, onPick: pickTemplate }),
          ),
        );
      }
    }
  }

  /** The category filters: "All apps", then the catalog's categories, the chosen one tinted. */
  function categoryFilters(snapshot: StoreSnapshot): m.Vnode {
    const choices: { key: string | null; title: string }[] = [
      { key: null, title: "All apps" },
      ...snapshot.categories,
    ];
    return m(
      "div",
      { class: "mt-4 flex flex-wrap gap-1", role: "group", "aria-label": "Show apps in", "data-category-filters": "" },
      choices.map((choice) => {
        const isChosen = choice.key === category;
        return m(
          Button,
          {
            key: choice.key ?? "all",
            variant: "ghost",
            selected: isChosen,
            "aria-pressed": isChosen ? "true" : "false",
            "data-category": choice.key ?? "all",
            onclick: () => {
              category = choice.key;
            },
          },
          choice.title,
        );
      }),
    );
  }

  function searchField(): m.Vnode {
    const placeholder = section === "apps" ? "Search apps" : "Search templates";
    return m("div", { class: "relative min-w-0 flex-1" }, [
      m(
        "span",
        { class: "pointer-events-none absolute inset-y-0 left-3 flex items-center text-faint" },
        m.trust(icon("search", { size: FIELD_GLYPH_SIZE })),
      ),
      m("input", {
        type: "text",
        "data-app-store-search": "",
        "aria-label": placeholder,
        placeholder,
        value: query,
        class: inputClass({ extra: "pl-9" }),
        oninput: (event: InputEvent) => (query = (event.target as HTMLInputElement).value),
        onkeydown: (event: KeyboardEvent) => {
          if (event.key === "Escape" && query !== "") {
            event.preventDefault();
            query = "";
          }
        },
      }),
    ]);
  }

  function header(snapshot: StoreSnapshot | null): m.Children {
    const storeInstalled = snapshot === null ? 0 : snapshot.apps.filter((app) => app.status === "INSTALLED").length;
    return [
      m("header", { class: "flex flex-col gap-1" }, [
        m("div", { class: "flex items-center justify-between gap-3" }, [
          m("h1", { class: "type-heading-lg m-0 text-primary" }, "App Store"),
          snapshot === null
            ? null
            : m(Button, { extra: "app-store-add-app", onclick: () => (isAdding = true) }, "Add app"),
        ]),
        m(
          "p",
          { class: "type-body m-0 text-secondary" },
          "Open-source apps to install in one click, and templates other people have published.",
        ),
      ]),
      snapshot === null
        ? null
        : m("div", { class: "mt-5 flex gap-7 border-b border-default pb-4", "data-summary": "" }, [
            stat(formatMb(snapshot.room.disk_free_mb), "Disk free", "disk"),
            stat(formatMb(snapshot.room.memory_available_mb), "Memory available", "memory"),
            stat(String(storeInstalled), "Installed from the store", "installed"),
          ]),
      m("div", { class: "mt-5 flex items-center gap-3" }, [
        m(SegmentedControl<Section>, {
          label: "Section",
          segments: [
            { value: "apps", label: "Apps" },
            { value: "templates", label: "Templates" },
          ],
          value: section,
          onSelect: (value: Section) => {
            section = value;
            query = "";
          },
        }),
        searchField(),
      ]),
    ];
  }

  async function submitRepoAdd(pendingId: string, job: (id: string) => Promise<string | null>): Promise<void> {
    isSubmittingRepoAdd = true;
    const reason = await job(pendingId);
    isSubmittingRepoAdd = false;
    if (reason === null) settledRepoAdds.add(pendingId);
    repoAddError = reason === null ? null : { id: pendingId, reason };
    m.redraw();
  }

  /** The oldest app from GitHub waiting for confirmation, unless another dialog is up or it was put off. */
  function repoAddView(snapshot: StoreSnapshot): m.Children {
    if (dialog !== null || isAdding) return null;
    const pending = snapshot.pending_repo_adds.find((candidate) => !settledRepoAdds.has(candidate.id));
    if (pending === undefined) return null;
    // Keyed (in a fragment of its own), so each pending add gets a dialog of its own, opening with the focus on its
    // way out.
    return [
      m(RepoAddDialog, {
        key: pending.id,
        pending,
        room: snapshot.room,
        error: repoAddError?.id === pending.id ? repoAddError.reason : null,
        isSubmitting: isSubmittingRepoAdd,
        onLater: () => {
          settledRepoAdds.add(pending.id);
          repoAddError = null;
        },
        onDiscard: () => void submitRepoAdd(pending.id, discardRepoAdd),
        onConfirm: () => void submitRepoAdd(pending.id, confirmRepoAdd),
      }),
    ];
  }

  function dialogView(snapshot: StoreSnapshot): m.Children {
    if (dialog === null) return null;
    const { kind, slug } = dialog;
    const app = snapshot.apps.find((candidate) => candidate.slug === slug);
    if (app === undefined) return null;
    if (kind === "remove") {
      return m(RemoveDialog, {
        app,
        error: dialogError,
        isSubmitting,
        onCancel: closeDialog,
        onConfirm: () =>
          void submit(async () => {
            const refusal = await removeAddedApp(slug);
            if (refusal === null) detailSlug = null;
            return refusal;
          }),
      });
    }
    if (kind === "update") {
      const version = dialog.version ?? "";
      return m(UpdateDialog, {
        app,
        version,
        error: dialogError,
        isSubmitting,
        onCancel: closeDialog,
        onConfirm: () => void submit(() => requestUpdate(slug, version)),
      });
    }
    if (kind === "install") {
      return m(InstallDialog, {
        app,
        room: snapshot.room,
        error: dialogError,
        isSubmitting,
        onCancel: closeDialog,
        onConfirm: () => void submit(() => requestInstall(slug)),
      });
    }
    return m(UninstallDialog, {
      app,
      error: dialogError,
      isSubmitting,
      onCancel: closeDialog,
      onConfirm: (isDeletingData) => void submit(() => requestUninstall(slug, isDeletingData)),
    });
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const snapshot = attrs.store.kind === "loaded" ? attrs.store.snapshot : null;
      const detailApp =
        snapshot === null || detailSlug === null
          ? null
          : (snapshot.apps.find((app) => app.slug === detailSlug) ?? null);
      let body: m.Children;
      if (detailApp !== null && snapshot !== null) {
        body = m(AppDetail, {
          app: detailApp,
          room: snapshot.room,
          onBack: () => (detailSlug = null),
          onRemove: (app) => openDialog("remove", app),
          onUpdate: (app, version) => openDialog("update", app, version),
          onStartWithText: attrs.onStartWithText,
          ...actions(attrs),
        });
      } else if (detailTemplate !== null) {
        body = m(StoreTemplateDetail, {
          template: detailTemplate,
          size: templateSize,
          onBack: () => (detailTemplate = null),
          onStartWithText: attrs.onStartWithText,
        });
      } else if (section === "templates") {
        body = [header(snapshot), templatesSection(attrs.templates)];
      } else if (snapshot === null) {
        body = [
          header(null),
          m(
            "p",
            { class: "mt-6 type-body text-faint", "data-store-status": attrs.store.kind },
            attrs.store.kind === "failed" ? "Failed to load the store." : "Loading…",
          ),
        ];
      } else {
        body = [header(snapshot), categoryFilters(snapshot), appsSection(snapshot, attrs)];
      }
      return m(
        "main",
        { class: "app-store-page h-screen w-full overflow-y-auto bg-page px-6 py-5 text-primary" },
        m("div", { class: "@container mx-auto w-full max-w-4xl" }, body),
        snapshot === null ? null : dialogView(snapshot),
        snapshot === null ? null : repoAddView(snapshot),
        snapshot === null || !isAdding
          ? null
          : m(AddAppDialog, {
              room: snapshot.room,
              finder: {
                start: startFindCommand,
                poll: fetchFinderJob,
                cancel: cancelFindCommand,
                screenshotUrl: finderScreenshotUrl,
                logUrl: finderLogUrl,
              },
              onCheck: checkAddedApp,
              onAdd: addApp,
              onStartWithText: attrs.onStartWithText,
              onClose: () => (isAdding = false),
            }),
      );
    },
  };
}
