/**
 * The questions the app stops to ask: whether to delete, what to do about names already taken by an upload, and
 * what to call a file renamed from its own page. Each is a promise its caller awaits; one dialog shows at a time.
 */

import m from "mithril";

import { Button } from "@imbue/workspace-ui/src/components/Button";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { MODAL_MESSAGE_CLASS, Modal } from "@imbue/workspace-ui/src/components/Modal";

import type { Entry } from "../models/dufs";
import type { ConflictChoice } from "../models/FilesModel";
import { invalidNameReason, stemLength } from "../models/paths";
import type { ConnectionRequest, Device, DroppedItem } from "../models/connections";
import { Dropdown } from "@imbue/workspace-ui/src/components/dropdown";

type Dialog =
  | { kind: "delete"; entries: readonly Entry[]; resolve: (confirmed: boolean) => void }
  | { kind: "replace"; names: readonly string[]; resolve: (choice: ConflictChoice) => void }
  | {
      kind: "rename";
      name: string;
      draft: string;
      error: string | null;
      resolve: (name: string | null) => void;
    }
  | {
      kind: "connect";
      devices: readonly Device[];
      draft: ConnectionRequest;
      isDropped: boolean;
      resolve: (ask: ConnectionRequest | null) => void;
    };

/** How many names a dialog lists before it says "and N more". */
const LISTED_NAMES = 5;

function listedNames(names: readonly string[]): m.Children {
  const shown = names.slice(0, LISTED_NAMES);
  const more = names.length - shown.length;
  return m("ul.files-dialog-names.mb-4.list-none.p-0.type-body.text-secondary", [
    shown.map((name) => m("li.truncate", { key: name }, name)),
    more > 0 ? m("li", `and ${more} more`) : null,
  ]);
}

export class Dialogs {
  current: Dialog | null = null;

  confirmDelete(entries: readonly Entry[]): Promise<boolean> {
    return new Promise((resolve) => this.show({ kind: "delete", entries, resolve }));
  }

  confirmReplace(names: readonly string[]): Promise<ConflictChoice> {
    return new Promise((resolve) => this.show({ kind: "replace", names, resolve }));
  }

  /** Ask for a new name for `name`; null when the user backs out. */
  askName(name: string): Promise<string | null> {
    return new Promise((resolve) => this.show({ kind: "rename", name, draft: name, error: null, resolve }));
  }

  /** Ask what to connect from which computer; null when the user backs out. */
  /** What to ask a computer to share; `dropped` is an item dragged in from it, of which a browser tells only the name. */
  askConnection(devices: readonly Device[], dropped: DroppedItem | null = null): Promise<ConnectionRequest | null> {
    const draft: ConnectionRequest = {
      device: devices[0]?.id ?? "",
      path: dropped === null ? "~/" : `~/${dropped.name}`,
      access: "read",
      sync: dropped === null || dropped.isFolder,
    };
    return new Promise((resolve) =>
      this.show({ kind: "connect", devices, draft, isDropped: dropped !== null, resolve }),
    );
  }

  private show(dialog: Dialog): void {
    this.dismiss();
    this.current = dialog;
    m.redraw();
  }

  /** Close whatever is open, answering it as a cancel. */
  dismiss(): void {
    const dialog = this.current;
    if (dialog === null) return;
    this.current = null;
    if (dialog.kind === "delete") dialog.resolve(false);
    else if (dialog.kind === "replace") dialog.resolve("cancel");
    else if (dialog.kind === "connect") dialog.resolve(null);
    else dialog.resolve(null);
  }

  private answer(settle: () => void): void {
    this.current = null;
    settle();
  }

  view(): m.Children {
    const dialog = this.current;
    if (dialog === null) return null;
    const dismiss = (): void => this.dismiss();
    if (dialog.kind === "delete") {
      const names = dialog.entries.map((entry) => entry.name);
      const title = names.length === 1 ? `Delete "${names[0]}"?` : `Delete ${names.length} items?`;
      return m(
        Modal,
        {
          title,
          onDismiss: dismiss,
          onEscape: dismiss,
          card: { "data-dialog": "delete", role: "alertdialog" },
          actions: [
            m(Button, { onclick: dismiss }, "Cancel"),
            m(
              Button,
              {
                variant: "destructive",
                "data-dialog-action": "confirm",
                oncreate: ({ dom }: { dom: Element }) => (dom as HTMLElement).focus(),
                onclick: () => this.answer(() => dialog.resolve(true)),
              },
              "Delete",
            ),
          ],
        },
        [
          names.length > 1 ? listedNames(names) : null,
          m("p", { class: MODAL_MESSAGE_CLASS }, "This can't be undone."),
        ],
      );
    }
    if (dialog.kind === "replace") {
      const title =
        dialog.names.length === 1
          ? `"${dialog.names[0]}" already exists here`
          : `${dialog.names.length} items already exist here`;
      return m(
        Modal,
        {
          title,
          onDismiss: dismiss,
          onEscape: dismiss,
          width: 460,
          card: { "data-dialog": "replace", role: "alertdialog" },
          actions: [
            m(Button, { onclick: dismiss }, "Cancel"),
            m(
              Button,
              { "data-dialog-action": "keep-both", onclick: () => this.answer(() => dialog.resolve("keep-both")) },
              "Keep both",
            ),
            m(
              Button,
              {
                variant: "primary",
                "data-dialog-action": "replace",
                oncreate: ({ dom }: { dom: Element }) => (dom as HTMLElement).focus(),
                onclick: () => this.answer(() => dialog.resolve("replace")),
              },
              "Replace",
            ),
          ],
        },
        [
          dialog.names.length > 1 ? listedNames(dialog.names) : null,
          m(
            "p",
            { class: MODAL_MESSAGE_CLASS },
            "Replace what's there with what you're adding, or keep both by giving the new ones a number.",
          ),
        ],
      );
    }
    if (dialog.kind === "connect") return this.connectView(dialog);
    const submit = (): void => {
      const name = dialog.draft.trim();
      const problem = invalidNameReason(name);
      if (problem !== null) {
        dialog.error = problem;
        return;
      }
      this.answer(() => dialog.resolve(name === dialog.name ? null : name));
    };
    return m(
      Modal,
      {
        title: "Rename",
        onDismiss: dismiss,
        onEscape: dismiss,
        card: { "data-dialog": "rename" },
        actions: [
          m(Button, { onclick: dismiss }, "Cancel"),
          m(Button, { variant: "primary", "data-dialog-action": "rename", onclick: submit }, "Rename"),
        ],
      },
      [
        m("input", {
          class: inputClass({ extra: "mb-2" }),
          value: dialog.draft,
          "aria-label": "New name",
          oncreate: ({ dom }: { dom: Element }) => {
            const input = dom as HTMLInputElement;
            input.focus();
            input.setSelectionRange(0, stemLength(dialog.name));
          },
          oninput: (event: InputEvent) => {
            dialog.draft = (event.target as HTMLInputElement).value;
            dialog.error = null;
          },
          onkeydown: (event: KeyboardEvent) => {
            if (event.key === "Enter") submit();
          },
        }),
        m("p.type-helper.mb-2.min-h-4.text-danger", dialog.error ?? ""),
      ],
    );
  }

  private connectView(dialog: Extract<Dialog, { kind: "connect" }>): m.Children {
    const dismiss = (): void => this.dismiss();
    const draft = dialog.draft;
    const choice = (value: "read" | "write", label: string, detail: string): m.Vnode =>
      m(
        "label.files-connect-access",
        {
          class: `flex cursor-pointer items-start gap-2 rounded-md border px-3 py-2 ${
            draft.access === value ? "border-accent bg-accent-light" : "border-default hover:bg-fill-hover"
          }`,
        },
        [
          m("input", {
            type: "radio",
            name: "files-connect-access",
            value,
            checked: draft.access === value,
            class: "mt-1",
            onchange: () => (draft.access = value),
          }),
          m("span", [
            m("span", { class: "block type-body text-primary" }, label),
            m("span", { class: "type-helper text-faint" }, detail),
          ]),
        ],
      );
    const device = dialog.devices.find((candidate) => candidate.id === draft.device);
    return m(
      Modal,
      {
        title: "Connect from your computer",
        onDismiss: dismiss,
        onEscape: dismiss,
        width: 440,
        card: { "data-dialog": "connect" },
        actions: [
          m(Button, { onclick: dismiss }, "Cancel"),
          m(
            Button,
            {
              variant: "primary",
              "data-dialog-action": "connect",
              disabled: draft.device === "",
              onclick: () => this.answer(() => dialog.resolve({ ...draft, path: draft.path.trim() || "~" })),
            },
            "Ask on my computer",
          ),
        ],
      },
      [
        dialog.devices.length === 0
          ? m("p", { class: MODAL_MESSAGE_CLASS }, "None of your computers is connected to Imbue Studio right now.")
          : dialog.devices.length === 1
            ? m("p", { class: "mb-3 type-helper text-secondary" }, `From ${device?.name ?? "your computer"}`)
            : m("div", { class: "mb-3" }, [
                m(Dropdown<string>, {
                  options: dialog.devices.map((candidate) => ({ value: candidate.id, label: candidate.name })),
                  value: draft.device,
                  placeholder: "Pick a computer",
                  "aria-label": "Computer",
                  onSelect: (value: string) => (draft.device = value),
                }),
              ]),
        m("label", { class: "mb-1 block type-helper font-medium text-secondary" }, "File or folder"),
        m("input.files-connect-path", {
          class: inputClass({ mono: true }),
          value: draft.path,
          placeholder: "~/Documents/Project",
          "aria-label": "File or folder",
          oncreate: ({ dom }: { dom: Element }) => {
            const input = dom as HTMLInputElement;
            input.focus();
            if (dialog.isDropped) input.setSelectionRange(2, 2);
          },
          oninput: (event: InputEvent) => (draft.path = (event.target as HTMLInputElement).value),
        }),
        m(
          "p.files-connect-hint",
          { class: "mt-1 mb-3 type-helper text-faint" },
          dialog.isDropped
            ? "A drop only tells Files the name, so check the folder it's in. Imbue Studio lets you correct it, or browse to it, before you approve."
            : "You can change it in Imbue Studio before you approve.",
        ),
        m("div", { class: "mb-3 flex flex-col gap-2" }, [
          choice("read", "Read only", "Files here can see it but not change it."),
          choice("write", "Read and change", "Changes made here reach your computer."),
        ]),
        m("label", { class: "mb-4 flex cursor-pointer items-start gap-2 type-body text-primary" }, [
          m("input.files-connect-sync", {
            type: "checkbox",
            checked: draft.sync,
            class: "mt-1",
            onchange: (event: Event) => (draft.sync = (event.target as HTMLInputElement).checked),
          }),
          m("span", [
            "Keep a synced copy here",
            m(
              "span",
              { class: "block type-helper text-faint" },
              "Folders only. Without one, it opens live from your computer while it's awake.",
            ),
          ]),
        ]),
      ],
    );
  }
}
