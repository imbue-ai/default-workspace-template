/**
 * The Apps section's tiles, drawn like the "Start something" tiles on Getting Started. A store tile is one app the
 * store offers: its icon, name and maker, one line on what it is, what it costs against what is free, and the one
 * thing to do with it now (install, watch it install, or try again). A workspace tile is one app this workspace
 * already has, with Open, and -- for an app the store installed -- what it uses now; its page has Uninstall.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import type { StoreApp, WorkspaceApp } from "../models/Store";
import { iconDataUrl, installLogUrl, isWorking } from "../models/Store";
import { diskText, formatMb, memoryText } from "../room";
import { appIcon, installProgress, verdictBadge } from "./AppParts";

const TILE_ICON_SIZE = 40;
const WORKSPACE_ICON_SIZE = 32;
const TILE_CLASS =
  "app-store-tile group flex flex-col gap-3 rounded-xl border border-default bg-surface p-4 text-left " +
  "transition-[scale,box-shadow] duration-300 ease-out";
export const ADDED_LABEL = "Added by you";
const ORIGIN_LABELS = { BUILT_IN: "Built in", MADE_HERE: "Made in this workspace", STORE: "App Store" } as const;

export interface AppActions {
  onPick: (app: StoreApp) => void;
  onInstall: (app: StoreApp) => void;
  onOpen: (url: string) => void;
  onUninstall: (app: StoreApp) => void;
}

function stop(handler: () => void): (event: Event) => void {
  return (event) => {
    event.stopPropagation();
    handler();
  };
}

function tileHeading(iconSrc: string, title: string, byline: string): m.Vnode {
  return m("div", { class: "flex items-center gap-3" }, [
    appIcon(iconSrc, TILE_ICON_SIZE),
    m("div", { class: "min-w-0" }, [
      m("div", { class: "type-label truncate text-primary" }, title),
      m("div", { class: "type-helper truncate text-faint" }, byline),
    ]),
  ]);
}

function description(text: string): m.Vnode {
  return m(
    "p",
    { class: "m-0 type-helper text-secondary transition-colors duration-300 ease-out group-hover:text-primary" },
    text,
  );
}

/** Disk and memory, the measured disk once installed, and the room badge while it can still be installed. */
export function costLine(app: StoreApp, isShowingVerdict: boolean): m.Vnode {
  return m(
    "div",
    { class: "flex flex-wrap items-center gap-x-4 gap-y-1 type-helper text-secondary", "data-cost": "" },
    [
      m("span", { "data-disk": "" }, ["Disk ", m("span", { class: "text-primary" }, diskText(app))]),
      m(
        "span",
        {
          "data-memory": "",
          title:
            app.memory_idle_mb === null ? undefined : `About ${formatMb(app.memory_idle_mb)} when nobody is using it`,
        },
        ["Memory ", m("span", { class: "text-primary" }, memoryText(app))],
      ),
      isShowingVerdict ? verdictBadge(app.verdict) : null,
    ],
  );
}

export interface StoreAppTileAttrs extends AppActions {
  app: StoreApp;
  /** The title of the app whose job is running, when it is another one. */
  busyWithTitle: string | null;
}

function storeFooter(attrs: StoreAppTileAttrs): m.Children {
  const { app, busyWithTitle } = attrs;
  if (isWorking(app) && app.status !== null) {
    return installProgress(app.status, app.has_log ? installLogUrl(app.slug) : null);
  }
  if (app.status === "FAILED") {
    return m("div", { class: "flex flex-col gap-2" }, [
      m("p", { class: "m-0 type-helper text-danger", "data-failure": "" }, app.failure_reason),
      m("div", { class: "flex items-center justify-between gap-2" }, [
        app.has_log
          ? m(
              "a",
              {
                class: "type-helper text-secondary hover:text-primary",
                href: installLogUrl(app.slug),
                target: "_blank",
                onclick: (event: Event) => event.stopPropagation(),
              },
              "Install log",
            )
          : m("span"),
        app.verdict === "WONT_FIT"
          ? null
          : m(Button, { sm: true, extra: "app-store-retry", onclick: stop(() => attrs.onInstall(app)) }, "Try again"),
      ]),
    ]);
  }
  if (app.verdict === "WONT_FIT")
    return m("p", { class: "m-0 type-helper text-danger", "data-blocked": "" }, app.blocked_reason);
  return m("div", { class: "flex items-center justify-between gap-2" }, [
    m(
      "span",
      { class: "type-helper text-faint" },
      busyWithTitle === null ? "" : `Starts after ${busyWithTitle} finishes`,
    ),
    m(
      Button,
      { variant: "primary", sm: true, extra: "app-store-install", onclick: stop(() => attrs.onInstall(app)) },
      "Install",
    ),
  ]);
}

export function StoreAppTile(): m.Component<StoreAppTileAttrs> {
  return {
    view(vnode) {
      const { app } = vnode.attrs;
      return m(
        "div",
        {
          "data-app": app.slug,
          role: "button",
          tabindex: 0,
          class: `${TILE_CLASS} cursor-pointer hover:shadow-overlay`,
          onclick: () => vnode.attrs.onPick(app),
          onkeydown: (event: KeyboardEvent) => {
            if (event.key === "Enter") vnode.attrs.onPick(app);
          },
        },
        [
          tileHeading(app.icon_url, app.title, app.is_added ? ADDED_LABEL : `${app.author} · ${app.license}`),
          description(app.description),
          costLine(app, app.status === null || app.status === "FAILED"),
          m("div", { class: "mt-auto" }, storeFooter(vnode.attrs)),
        ],
      );
    },
  };
}

/** An installed store app's line: where it came from and what it is using right now. */
export function storeAppSubtitle(app: StoreApp): string {
  const usage = app.memory_now_mb === null ? "not running" : `using ${formatMb(app.memory_now_mb)}`;
  return `${app.is_added ? ADDED_LABEL : ORIGIN_LABELS.STORE} · ${usage}`;
}

export interface WorkspaceAppTileAttrs extends AppActions {
  app: WorkspaceApp;
  /** The store's entry when the store installed it. */
  storeApp: StoreApp | null;
}

export function WorkspaceAppTile(): m.Component<WorkspaceAppTileAttrs> {
  return {
    view(vnode) {
      const { app, storeApp, onOpen, onPick } = vnode.attrs;
      const isUninstalling = storeApp?.status === "UNINSTALLING";
      return m(
        "div",
        {
          "data-workspace-app": app.name,
          title: app.description === "" ? undefined : app.description,
          class:
            "app-store-workspace-tile group flex items-center gap-3 rounded-xl border border-default bg-surface px-3 py-2.5 " +
            `transition-[scale,box-shadow] duration-300 ease-out ${storeApp === null ? "" : "cursor-pointer hover:shadow-overlay"}`,
          onclick: storeApp === null ? undefined : () => onPick(storeApp),
        },
        [
          appIcon(app.icon_svg === "" ? "" : iconDataUrl(app.icon_svg), WORKSPACE_ICON_SIZE),
          m("div", { class: "min-w-0 flex-1" }, [
            m("div", { class: "type-label truncate text-primary" }, app.display_name),
            m(
              "div",
              { class: "type-helper truncate text-faint" },
              isUninstalling
                ? "Uninstalling…"
                : storeApp === null
                  ? ORIGIN_LABELS[app.origin]
                  : storeAppSubtitle(storeApp),
            ),
          ]),
          isUninstalling
            ? m("span", { class: "spinner spinner--sm" })
            : m(Button, { sm: true, extra: "app-store-open", onclick: stop(() => onOpen(app.url)) }, "Open"),
        ],
      );
    },
  };
}
