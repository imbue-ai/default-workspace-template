/**
 * The questions the store asks before it changes anything: install this third-party app (what, from where, under
 * which license, at what cost, and that it is someone else's code), uninstall this one, keeping or deleting its
 * files, and add this app a chat worked out how to install from GitHub (the exact commands it will run). A refusal
 * from the store (it won't fit, it is busy) shows in the dialog that asked.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { MODAL_MESSAGE_CLASS, Modal } from "@imbue/workspace-ui/src/components/Modal";
import type { PendingRepoAdd, StoreApp, WorkspaceRoom } from "../models/Store";
import { buildText, formatMb, memoryText } from "../room";
import { BODY_SCROLL_CLASS, UNREVIEWED_WARNING } from "./AddAppDialog";
import { verdictBadge } from "./AppParts";

const INSTALL_DIALOG_WIDTH = 480;
export const THIRD_PARTY_WARNING =
  "This is third-party code from the open-source project above, not something this workspace wrote. It runs " +
  "inside your workspace and can read your files there.";

function fact(label: string, value: m.Children): m.Vnode[] {
  return [
    m("dt", { class: "type-helper text-faint" }, label),
    m("dd", { class: "m-0 type-body text-primary" }, value),
  ];
}

function refusal(error: string | null): m.Vnode | null {
  return error === null ? null : m("p", { class: "m-0 mb-4 type-helper text-danger", "data-refusal": "" }, error);
}

export interface InstallDialogAttrs {
  app: StoreApp;
  room: WorkspaceRoom;
  /** Why the store refused, once it has. */
  error: string | null;
  isSubmitting: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export function InstallDialog(): m.Component<InstallDialogAttrs> {
  return {
    view(vnode) {
      const { app, room, error, isSubmitting, onCancel, onConfirm } = vnode.attrs;
      return m(
        Modal,
        {
          title: `Install ${app.title}?`,
          width: INSTALL_DIALOG_WIDTH,
          onDismiss: onCancel,
          onEscape: onCancel,
          card: { role: "dialog", "aria-modal": "true", "data-dialog": "install" },
          actions: [
            m(Button, { onclick: onCancel }, "Cancel"),
            m(
              Button,
              {
                variant: "primary",
                extra: "app-store-confirm-install",
                disabled: isSubmitting || app.verdict === "WONT_FIT",
                onclick: onConfirm,
              },
              "Install",
            ),
          ],
        },
        [
          m("p", { class: MODAL_MESSAGE_CLASS }, "It installs in the background, then shows up with your other apps."),
          m("dl", { class: "m-0 mb-4 grid grid-cols-[6rem_1fr] gap-x-4 gap-y-1.5" }, [
            ...fact("What", [
              `${app.title} ${app.version}, ${app.source_label} `,
              m("code", { class: "font-mono type-helper" }, app.package_spec),
            ]),
            ...fact(
              "From",
              m(
                "a",
                {
                  class: "text-accent hover:underline",
                  href: app.source_url,
                  target: "_blank",
                  rel: "noopener noreferrer",
                },
                app.source_url.replace("https://", ""),
              ),
            ),
            ...fact("License", app.license),
            ...fact("Disk", [
              app.disk_mb === null ? "unknown " : `about ${formatMb(app.disk_mb)} `,
              m("span", { class: "type-helper text-faint" }, `(${formatMb(room.disk_free_mb)} free)`),
            ]),
            ...fact("Memory", [
              app.memory_in_use_mb === null || app.memory_idle_mb === null
                ? `${memoryText(app)} `
                : `about ${formatMb(app.memory_in_use_mb)} in use, ${formatMb(app.memory_idle_mb)} idle `,
              m("span", { class: "type-helper text-faint" }, `(${formatMb(room.memory_available_mb)} available)`),
            ]),
            ...(buildText(app) === "" ? [] : fact("Building", buildText(app))),
            ...fact("Room", verdictBadge(app.verdict)),
          ]),
          m(
            "p",
            { class: "m-0 mb-4 rounded-lg bg-warning-surface px-3 py-2 type-helper text-primary", "data-warning": "" },
            app.is_added ? UNREVIEWED_WARNING : THIRD_PARTY_WARNING,
          ),
          refusal(error ?? (app.blocked_reason === "" ? null : app.blocked_reason)),
        ],
      );
    },
  };
}

export interface UninstallDialogAttrs {
  app: StoreApp;
  error: string | null;
  isSubmitting: boolean;
  onCancel: () => void;
  onConfirm: (isDeletingData: boolean) => void;
}

export function UninstallDialog(): m.Component<UninstallDialogAttrs> {
  let isDeletingData = false;
  return {
    view(vnode) {
      const { app, error, isSubmitting, onCancel, onConfirm } = vnode.attrs;
      const freed = app.measured_disk_mb ?? app.disk_mb ?? 0;
      return m(
        Modal,
        {
          title: `Uninstall ${app.title}?`,
          onDismiss: onCancel,
          onEscape: onCancel,
          card: { role: "dialog", "aria-modal": "true", "data-dialog": "uninstall" },
          actions: [
            m(Button, { onclick: onCancel }, "Cancel"),
            m(
              Button,
              {
                variant: "destructive",
                extra: "app-store-confirm-uninstall",
                disabled: isSubmitting,
                onclick: () => onConfirm(isDeletingData),
              },
              "Uninstall",
            ),
          ],
        },
        [
          m(
            "p",
            { class: MODAL_MESSAGE_CLASS },
            `This stops ${app.title} and takes it off your desktop, freeing about ${formatMb(freed)}.`,
          ),
          m("label", { class: "mb-4 flex items-start gap-2 type-body text-primary" }, [
            m("input", {
              type: "checkbox",
              class: "mt-1",
              "data-delete-data": "",
              checked: isDeletingData,
              onchange: (event: Event) => (isDeletingData = (event.target as HTMLInputElement).checked),
            }),
            m("span", [
              "Also delete its files",
              m(
                "span",
                { class: "block type-helper text-secondary" },
                "Its notebooks, databases and settings. This can't be undone. Leave it unticked to keep them for a reinstall.",
              ),
            ]),
          ]),
          refusal(error),
        ],
      );
    },
  };
}

export interface RemoveDialogAttrs {
  app: StoreApp;
  error: string | null;
  isSubmitting: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

/** Take an added app out of the store: it is not installed, so this deletes what it left behind and the entry. */
export function RemoveDialog(): m.Component<RemoveDialogAttrs> {
  return {
    view(vnode) {
      const { app, error, isSubmitting, onCancel, onConfirm } = vnode.attrs;
      return m(
        Modal,
        {
          title: `Remove ${app.title} from the store?`,
          onDismiss: onCancel,
          onEscape: onCancel,
          card: { role: "dialog", "aria-modal": "true", "data-dialog": "remove" },
          actions: [
            m(Button, { onclick: onCancel }, "Cancel"),
            m(
              Button,
              {
                variant: "destructive",
                extra: "app-store-confirm-remove",
                disabled: isSubmitting,
                onclick: onConfirm,
              },
              "Remove",
            ),
          ],
        },
        [
          m(
            "p",
            { class: MODAL_MESSAGE_CLASS },
            "It goes off this list, along with its install log and any files it kept when it was uninstalled. To use it again, add it again.",
          ),
          refusal(error),
        ],
      );
    },
  };
}

export interface RepoAddDialogAttrs {
  pending: PendingRepoAdd;
  room: WorkspaceRoom;
  error: string | null;
  isSubmitting: boolean;
  /** Close it for now; it stays waiting and comes back on the next visit. */
  onLater: () => void;
  onDiscard: () => void;
  onConfirm: () => void;
}

function commandBlock(lines: string[], marker: string): m.Vnode {
  return m(
    "pre",
    {
      class:
        "m-0 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-md bg-surface-secondary px-2 py-1.5 font-mono type-helper text-primary",
      [marker]: "",
    },
    lines.join("\n"),
  );
}

/** Add an app a chat worked out how to install from one commit of a GitHub repository: what it is, where from,
 *  the exact commands it will run, what it costs, and that nobody reviewed it. Nothing is installed until "Add to
 *  store". */
export function RepoAddDialog(): m.Component<RepoAddDialogAttrs> {
  return {
    view(vnode) {
      const { pending, room, error, isSubmitting, onLater, onDiscard, onConfirm } = vnode.attrs;
      const building =
        pending.build_memory_mb === null && pending.build_disk_mb === null
          ? null
          : [
              pending.build_memory_mb === null ? null : `${formatMb(pending.build_memory_mb)} of memory`,
              pending.build_disk_mb === null ? null : `${formatMb(pending.build_disk_mb)} of disk`,
            ]
              .filter((part) => part !== null)
              .join(" and ");
      const isUpdate = pending.replaces !== "";
      return m(
        Modal,
        {
          title: isUpdate ? `Update ${pending.title} to ${pending.version}?` : `Add ${pending.title} to the store?`,
          width: INSTALL_DIALOG_WIDTH,
          onDismiss: onLater,
          onEscape: onLater,
          card: { role: "dialog", "aria-modal": "true", "data-dialog": "repo-add" },
          actions: [
            m(Button, { extra: "app-store-discard-repo-add", disabled: isSubmitting, onclick: onDiscard }, "Discard"),
            m(Button, { onclick: onLater }, "Not now"),
            m(
              Button,
              {
                variant: "primary",
                extra: "app-store-confirm-repo-add",
                disabled: isSubmitting || pending.verdict === "WONT_FIT",
                onclick: onConfirm,
              },
              isUpdate ? "Update" : "Add to store",
            ),
          ],
        },
        // The body scrolls on its own, so the buttons stay on screen below a tall screenshot.
        m("div", { class: BODY_SCROLL_CLASS, "data-dialog-body": "" }, [
          m(
            "p",
            { class: MODAL_MESSAGE_CLASS },
            isUpdate
              ? `A chat found and tried version ${pending.version} (you have ${pending.installed_version}). The app ` +
                  "stops while it updates, keeps its files and settings, and goes back to the version you have if " +
                  "the new one doesn't install or start."
              : `${pending.about} A chat worked out how to install it; adding it installs it in the background.`,
          ),
          pending.screenshot_url === null
            ? m(
                "p",
                { class: "m-0 mb-4 type-helper text-secondary", "data-no-screenshot": "" },
                "No picture of it running was taken when it was tried out.",
              )
            : m("figure", { class: "m-0 mb-4" }, [
                m("img", {
                  src: apiUrl(pending.screenshot_url),
                  alt: `${pending.title} running when it was tried out`,
                  class: "block w-full rounded-lg border border-default",
                  "data-screenshot": "",
                }),
                m(
                  "figcaption",
                  { class: "mt-1 type-helper text-faint" },
                  "How it looked when the chat tried it out, in a window like the one it will open in.",
                ),
              ]),
          m("dl", { class: "m-0 mb-4 grid grid-cols-[6rem_1fr] gap-x-4 gap-y-1.5" }, [
            ...fact("What", pending.source_label),
            ...fact("From", [
              m(
                "a",
                {
                  class: "text-accent hover:underline",
                  href: pending.commit_url,
                  target: "_blank",
                  rel: "noopener noreferrer",
                  "data-commit-link": "",
                },
                `${pending.repository} at ${pending.commit.slice(0, 12)}`,
              ),
            ]),
            ...fact("Disk", [
              pending.disk_mb === null ? "unknown " : `about ${formatMb(pending.disk_mb)} `,
              m("span", { class: "type-helper text-faint" }, `(${formatMb(room.disk_free_mb)} free)`),
            ]),
            ...fact("Memory", [
              pending.memory_in_use_mb === null ? "unknown " : `about ${formatMb(pending.memory_in_use_mb)} in use `,
              m("span", { class: "type-helper text-faint" }, `(${formatMb(room.memory_available_mb)} available)`),
            ]),
            ...(building === null ? [] : fact("Building", `needs about ${building} while it installs`)),
            ...fact("Room", verdictBadge(pending.verdict)),
          ]),
          m("p", { class: "m-0 mb-1 type-helper text-secondary" }, "Installing runs:"),
          commandBlock(pending.install_commands, "data-install-commands"),
          m("p", { class: "m-0 mb-1 mt-3 type-helper text-secondary" }, "Then it starts with:"),
          commandBlock([pending.start_command], "data-start-command"),
          pending.basis === "" ? null : m("p", { class: "m-0 mt-3 type-helper text-faint" }, pending.basis),
          m(
            "p",
            {
              class: "m-0 my-4 rounded-lg bg-warning-surface px-3 py-2 type-helper text-primary",
              "data-warning": "",
            },
            UNREVIEWED_WARNING,
          ),
          refusal(error ?? (pending.blocked_reason === "" ? null : pending.blocked_reason)),
        ]),
      );
    },
  };
}

export interface UpdateDialogAttrs {
  app: StoreApp;
  version: string;
  error: string | null;
  isSubmitting: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

/** Update an installed app: from which version to which, that it stops while it updates, that its files are kept,
 *  and that the version it has is put back if the new one fails. */
export function UpdateDialog(): m.Component<UpdateDialogAttrs> {
  return {
    view(vnode) {
      const { app, version, error, isSubmitting, onCancel, onConfirm } = vnode.attrs;
      return m(
        Modal,
        {
          title: `Update ${app.title} to ${version}?`,
          width: INSTALL_DIALOG_WIDTH,
          onDismiss: onCancel,
          onEscape: onCancel,
          card: { role: "dialog", "aria-modal": "true", "data-dialog": "update" },
          actions: [
            m(Button, { onclick: onCancel }, "Cancel"),
            m(
              Button,
              { variant: "primary", extra: "app-store-confirm-update", disabled: isSubmitting, onclick: onConfirm },
              "Update",
            ),
          ],
        },
        [
          m(
            "p",
            { class: MODAL_MESSAGE_CLASS },
            `${app.title} goes from ${app.installed_version ?? "the version you have"} to ${version}. It stops while ` +
              "the new version installs, then starts again. Its files and settings are kept. If the new version " +
              "doesn't install or start, the version you have now is put back.",
          ),
          app.is_added
            ? m(
                "p",
                {
                  class: "m-0 mb-4 rounded-lg bg-warning-surface px-3 py-2 type-helper text-primary",
                  "data-warning": "",
                },
                UNREVIEWED_WARNING,
              )
            : null,
          refusal(error),
        ],
      );
    },
  };
}
