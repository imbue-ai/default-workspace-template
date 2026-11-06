/**
 * The page behind a store app's tile, laid out like a template's detail page on Getting Started: its icon large,
 * what it is, what it needs against what the workspace has free, exactly what gets installed and from where, and
 * what to do with it now.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { AddedWith, NewestVersionAnswer, StoreApp, WorkspaceRoom } from "../models/Store";
import { fetchNewestVersion, githubUpdateMessage, installLogUrl, isRemovable, isWorking } from "../models/Store";
import { VERDICT_NOTES, buildText, formatMb } from "../room";
import type { AppActions } from "./AppTile";
import { appIcon, installProgress, roomMeter, verdictBadge } from "./AppParts";

const DETAIL_ICON_SIZE = 128;
const SMALL_GLYPH_SIZE = 14;
const BACK_GLYPH_SIZE = 16;

export interface AppDetailAttrs extends AppActions {
  app: StoreApp;
  room: WorkspaceRoom;
  onBack: () => void;
  /** Take an added app out of the store (only offered for one). */
  onRemove: (app: StoreApp) => void;
  /** Ask to update an installed app to ``version`` (it is confirmed first). */
  onUpdate: (app: StoreApp, version: string) => void;
  /** Open a chat with this message: checking an app added from GitHub for a newer version. */
  onStartWithText: (text: string) => void;
  /** Looks the newest version up; the store's by default. */
  checkNewest?: (slug: string) => Promise<NewestVersionAnswer>;
}

type UpdateCheck = { kind: "idle" } | { kind: "checking" } | NewestVersionAnswer;

function roomColumn(label: string, neededMb: number, freeMb: number, freeWord: string): m.Vnode {
  return m("div", { class: "min-w-0 flex-1" }, [
    m("div", { class: "type-helper text-faint" }, label),
    m("div", { class: "mt-0.5 type-body text-primary" }, [
      m("span", { class: "font-semibold" }, formatMb(neededMb)),
      m("span", { class: "type-helper text-secondary" }, ` of ${formatMb(freeMb)} ${freeWord}`),
    ]),
    roomMeter(neededMb, freeMb),
  ]);
}

function removeControl(app: StoreApp, onRemove: (app: StoreApp) => void): m.Vnode {
  const isAllowed = isRemovable(app);
  return m("div", { class: "mt-4 flex flex-wrap items-center gap-3 border-t border-default pt-4" }, [
    m(
      Button,
      {
        variant: "ghost-destructive",
        sm: true,
        extra: "app-store-remove",
        disabled: !isAllowed,
        onclick: () => onRemove(app),
      },
      "Remove from store",
    ),
    m(
      "span",
      { class: "type-helper text-secondary" },
      isAllowed
        ? "Takes it off this list, with anything it left behind."
        : "Uninstall it first; then it can be removed from the store.",
    ),
  ]);
}

function unknownColumn(label: string, value: string, hook: string): m.Vnode {
  return m("div", { class: "min-w-0 flex-1", [hook]: "" }, [
    m("div", { class: "type-helper text-faint" }, label),
    m("div", { class: "mt-0.5 type-body text-secondary" }, value),
  ]);
}

/** What an added app was added with: where from, the pinned version, and its command. */
function addedWithSection(added: AddedWith): m.Vnode {
  return m("section", { class: "mt-5", "data-added-with": "" }, [
    m("h4", { class: "type-section m-0 text-faint" }, "Added by you with"),
    m("dl", { class: "m-0 mt-2 grid grid-cols-[9rem_1fr] gap-x-4 gap-y-1.5" }, [
      ...fact("Source", [
        `${added.registry}: `,
        m(
          "a",
          {
            class: "text-accent hover:underline",
            href: added.registry_url,
            target: "_blank",
            rel: "noopener noreferrer",
          },
          added.package,
        ),
      ]),
      ...fact("Pinned version", `${added.version} (repairs and reinstalls keep it)`),
      ...fact("Start command", m("code", { class: "font-mono type-helper break-all" }, added.command.join(" "))),
      ...(added.python === null ? [] : fact("Python", added.python)),
      ...(added.package_count === null ? [] : fact("Packages", `${added.package_count} in all`)),
    ]),
    m("p", { class: "type-helper m-0 mt-2 text-secondary" }, "Nobody has reviewed this package for the store."),
  ]);
}

function fact(label: string, value: m.Children): m.Vnode[] {
  return [
    m("dt", { class: "type-helper text-faint" }, label),
    m("dd", { class: "m-0 type-body text-primary" }, value),
  ];
}

function link(href: string, label: string): m.Vnode {
  return m(
    "a",
    {
      class: "inline-flex items-center gap-1.5 type-helper text-secondary hover:text-primary",
      href,
      target: "_blank",
      rel: "noopener noreferrer",
    },
    [m("span", label), m.trust(icon("external-link", { size: SMALL_GLYPH_SIZE }))],
  );
}

function actions(attrs: AppDetailAttrs): m.Children {
  const { app } = attrs;
  if (isWorking(app) && app.status !== null) {
    return m(
      "div",
      { class: "w-full max-w-sm" },
      installProgress(app.status, app.has_log ? installLogUrl(app.slug) : null),
    );
  }
  if (app.status === "INSTALLED") {
    return [
      app.app_url === null
        ? null
        : m(
            Button,
            { variant: "primary", extra: "app-store-open", onclick: () => attrs.onOpen(app.app_url ?? "") },
            `Open ${app.title}`,
          ),
      m(
        Button,
        { variant: "ghost-destructive", extra: "app-store-uninstall", onclick: () => attrs.onUninstall(app) },
        "Uninstall",
      ),
    ];
  }
  return m(
    Button,
    {
      variant: "primary",
      extra: "app-store-install",
      disabled: app.verdict === "WONT_FIT",
      onclick: () => attrs.onInstall(app),
    },
    app.status === "FAILED" ? "Try again" : `Install ${app.title}`,
  );
}

/** What an installed app takes on disk, and what it is using right now. */
export function installedNote(app: StoreApp): string {
  const disk =
    app.measured_disk_mb === null ? "Installed" : `Installed, it takes ${formatMb(app.measured_disk_mb)} of disk`;
  const memory =
    app.memory_now_mb === null
      ? "it isn't running right now (it stops a minute after its window closes)"
      : `it is using ${formatMb(app.memory_now_mb)} of memory right now`;
  return `${disk}; ${memory}.`;
}

function roomNote(app: StoreApp): m.Vnode {
  if (app.status === "INSTALLED") {
    return m("p", { class: "type-helper m-0 mt-2 text-secondary", "data-installed-note": "" }, installedNote(app));
  }
  return m("div", { class: "mt-2 flex items-center gap-2" }, [
    verdictBadge(app.verdict),
    m(
      "span",
      { class: `type-helper ${app.blocked_reason === "" ? "text-secondary" : "text-danger"}`, "data-room-note": "" },
      app.blocked_reason === "" ? VERDICT_NOTES[app.verdict] : app.blocked_reason,
    ),
  ]);
}

/** The installed version, a newer one when known, and how to get it. */
function updateSection(attrs: AppDetailAttrs, check: UpdateCheck, onCheck: () => void): m.Children {
  const { app } = attrs;
  if (app.status !== "INSTALLED" || app.update_kind === null) return null;
  const updateButton = (version: string): m.Vnode =>
    m(
      Button,
      { sm: true, variant: "primary", extra: "app-store-update", onclick: () => attrs.onUpdate(app, version) },
      `Update to ${version}`,
    );
  let status: m.Children;
  let control: m.Children;
  if (app.update_version !== null) {
    status = `Version ${app.update_version} is available.`;
    control = updateButton(app.update_version);
  } else if (app.update_kind === "GITHUB") {
    status = "A chat looks for the project's newest stable version, tries it out, and asks you before updating.";
    control = m(
      Button,
      { sm: true, extra: "app-store-github-update", onclick: () => attrs.onStartWithText(githubUpdateMessage(app)) },
      "Check for a newer version",
    );
  } else if (check.kind === "checked") {
    status = check.isNewer ? `Version ${check.newestVersion} is available.` : "This is the newest version.";
    control = check.isNewer ? updateButton(check.newestVersion) : null;
  } else {
    status =
      check.kind === "refused"
        ? m("span", { class: "text-danger" }, check.reason)
        : app.update_kind === "CATALOG"
          ? "Newer versions come with your workspace's updates."
          : "";
    control = m(
      Button,
      { sm: true, extra: "app-store-check-update", disabled: check.kind === "checking", onclick: onCheck },
      check.kind === "checking" ? "Checking…" : "Check for updates",
    );
  }
  return m("section", { class: "mt-5", "data-updates": "" }, [
    m("h4", { class: "type-section m-0 text-faint" }, "Version"),
    m(
      "p",
      { class: "type-body m-0 mt-2 text-primary", "data-installed-version": "" },
      `${app.installed_version} installed`,
    ),
    app.failure_reason === ""
      ? null
      : m("p", { class: "type-helper m-0 mt-1 text-danger", "data-update-failure": "" }, app.failure_reason),
    m("div", { class: "mt-2 flex flex-wrap items-center gap-3" }, [
      control,
      m("span", { class: "type-helper text-secondary", "data-update-status": "" }, status),
    ]),
  ]);
}

export function AppDetail(): m.Component<AppDetailAttrs> {
  let check: UpdateCheck = { kind: "idle" };
  // What the check is about: the app, at the version it had. Another app, or the same one updated, starts over.
  let checkedFor = "";
  return {
    view(vnode) {
      const subject = `${vnode.attrs.app.slug}@${vnode.attrs.app.installed_version ?? ""}`;
      if (checkedFor !== subject) {
        check = { kind: "idle" };
        checkedFor = subject;
      }
      const onCheck = (): void => {
        check = { kind: "checking" };
        void (vnode.attrs.checkNewest ?? fetchNewestVersion)(vnode.attrs.app.slug).then((answer) => {
          if (checkedFor === subject) check = answer;
          m.redraw();
        });
      };
      const { app, room, onBack } = vnode.attrs;
      return m("section", { class: "app-store-detail", "data-app-detail": app.slug }, [
        m(Button, { variant: "ghost", sm: true, "aria-label": "Back", extra: "app-store-back", onclick: onBack }, [
          m.trust(icon("chevron-left", { size: BACK_GLYPH_SIZE })),
          m("span", "Back"),
        ]),
        m("div", { class: "mt-4 flex items-start gap-6 max-md:flex-col" }, [
          m(
            "div",
            {
              // The icon on its own: it is already a tile, so no frame around it.
              class: "shrink-0",
              "data-detail-icon": "",
            },
            appIcon(app.icon_url, DETAIL_ICON_SIZE),
          ),
          m("div", { class: "min-w-0 flex-1" }, [
            m("h2", { class: "type-heading m-0 text-primary" }, app.title),
            m("p", { class: "type-helper m-0 mt-1 text-secondary" }, `by ${app.author}`),
            m("p", { class: "type-body mt-4 text-primary" }, app.about),
            app.status === "FAILED"
              ? m("p", { class: "type-body mt-4 text-danger", "data-failure": "" }, app.failure_reason)
              : null,
            m("section", { class: "mt-5" }, [
              m("h4", { class: "type-section m-0 text-faint" }, "Room"),
              m("div", { class: "mt-2 flex gap-6 max-sm:flex-col" }, [
                app.disk_mb === null
                  ? unknownColumn("Disk it needs", "unknown", "data-disk-unknown")
                  : roomColumn(
                      app.is_added ? "Disk it needs (estimate)" : "Disk it needs",
                      app.disk_mb,
                      room.disk_free_mb,
                      "free",
                    ),
                app.memory_in_use_mb !== null
                  ? roomColumn("Memory while in use", app.memory_in_use_mb, room.memory_available_mb, "available")
                  : app.peak_memory_mb !== null
                    ? roomColumn("Memory (measured peak)", app.peak_memory_mb, room.memory_available_mb, "available")
                    : unknownColumn("Memory", "unknown until it runs", "data-memory-unknown"),
              ]),
              app.memory_idle_mb === null
                ? null
                : m(
                    "p",
                    { class: "type-helper m-0 mt-2 text-secondary", "data-memory-idle": "" },
                    `About ${formatMb(app.memory_idle_mb)} when nobody is using it. The fit is checked against the in-use figure.`,
                  ),
              app.peak_memory_mb === null || app.memory_in_use_mb === null
                ? null
                : m(
                    "p",
                    { class: "type-helper m-0 mt-1 text-secondary", "data-memory-peak": "" },
                    `Measured here: it held up to ${formatMb(app.peak_memory_mb)} in the first minutes of a run.`,
                  ),
              buildText(app) === ""
                ? null
                : m("p", { class: "type-helper m-0 mt-1 text-secondary", "data-build-needs": "" }, buildText(app)),
              roomNote(app),
              m("details", { class: "mt-2 type-helper text-secondary", "data-basis": "" }, [
                m("summary", { class: "cursor-pointer" }, "How these figures were worked out"),
                m("p", { class: "m-0 mt-1" }, app.resources_basis),
              ]),
            ]),
            updateSection(vnode.attrs, check, onCheck),
            app.added_with === null ? null : addedWithSection(app.added_with),
            m("section", { class: "mt-5" }, [
              m("h4", { class: "type-section m-0 text-faint" }, "Details"),
              m("dl", { class: "m-0 mt-2 grid grid-cols-[9rem_1fr] gap-x-4 gap-y-1.5" }, [
                ...fact("Installs", [
                  app.source_label,
                  " ",
                  m("code", { class: "font-mono type-helper" }, app.package_spec),
                ]),
                ...fact("License", app.license),
                ...fact("Its files", `App Store › ${app.slug} › data, in Files`),
              ]),
            ]),
            m("div", { class: "mt-5 flex flex-wrap gap-x-5 gap-y-2" }, [
              link(app.homepage_url, "Project home page"),
              link(app.source_url, "View the source"),
              app.has_log ? link(installLogUrl(app.slug), "Install log") : null,
            ]),
            m("div", { class: "mt-6 flex flex-wrap items-center gap-2" }, actions(vnode.attrs)),
            app.is_added ? removeControl(app, vnode.attrs.onRemove) : null,
          ]),
        ]),
      ]);
    },
  };
}
