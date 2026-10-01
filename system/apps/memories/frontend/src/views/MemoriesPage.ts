/**
 * The page, top to bottom: how many things the Claude chats remember; who can see the notes (every Claude chat
 * here, no other chat type, no other workspace, and the workspace's backups, which keep deleted notes until they
 * expire); then the notes grouped by what they are about, each with who wrote it, how many chats read it, the file
 * as it is on disk, and Edit and Delete. Delete asks first, saying what still holds a copy afterwards.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { badgeClass } from "@imbue/workspace-ui/src/components/Badge";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { makeNoticeDialog } from "@imbue/workspace-ui/src/components/NoticeDialog";
import type { BackupRetention, Note, NotesDocument, NotesState, NoteType } from "../models/notes";
import {
  attributionLine,
  backupsDetail,
  backupsLine,
  bodyBlocks,
  countLabel,
  deleteBackupsWarning,
  deletedStatus,
  formatDate,
} from "./format";

const SECTION_HEADING_CLASS = "type-section text-faint";
const DETAILS_CLASS =
  "memories-details overflow-x-auto rounded-md bg-surface-secondary p-3 font-mono type-helper text-secondary";
const DISCLOSE_CLASS = "memories-disclose self-start cursor-pointer type-helper text-accent hover:underline";
const NoticeDialog = makeNoticeDialog();

const GROUPS: readonly { readonly type: NoteType; readonly label: string }[] = [
  { type: "USER", label: "About you" },
  { type: "FEEDBACK", label: "How you like things done" },
  { type: "PROJECT", label: "What you're working on" },
  { type: "REFERENCE", label: "Where things are" },
  { type: "OTHER", label: "Other notes" },
];

export interface MemoriesPageAttrs {
  readonly state: NotesState;
  readonly onSave: (note: Note, description: string, body: string) => Promise<string | null>;
  readonly onDelete: (note: Note) => Promise<string | null>;
}

interface Draft {
  readonly fileName: string;
  description: string;
  body: string;
}

export function MemoriesPage(): m.Component<MemoriesPageAttrs> {
  const openDetails = new Set<string>();
  let draft: Draft | null = null;
  let pendingDelete: Note | null = null;
  let isBusy = false;
  let statusMessage: string | null = null;

  function toggle(id: string): void {
    if (openDetails.has(id)) openDetails.delete(id);
    else openDetails.add(id);
  }

  function disclose(id: string, closedLabel: string, openLabel: string): m.Vnode {
    const isOpen = openDetails.has(id);
    return m(
      "button",
      { type: "button", class: DISCLOSE_CLASS, "aria-expanded": String(isOpen), onclick: () => toggle(id) },
      isOpen ? openLabel : closedLabel,
    );
  }

  async function run(action: () => Promise<string | null>, success: string): Promise<void> {
    isBusy = true;
    statusMessage = null;
    m.redraw();
    const error = await action();
    isBusy = false;
    statusMessage = error ?? success;
    m.redraw();
  }

  function whoCanSee(document: NotesDocument): m.Vnode {
    const row = (tone: "success" | "neutral" | "warning", label: string, text: string): m.Vnode =>
      m("div", { class: "grid grid-cols-[96px_minmax(0,1fr)] items-baseline gap-3" }, [
        m("span", { class: `justify-self-start ${badgeClass(tone)}` }, label),
        m("span", { class: "type-body text-primary" }, text),
      ]);
    return m("section", { class: "flex flex-col gap-2 rounded-lg border border-default bg-surface-secondary p-4" }, [
      m("span", { class: "type-label text-primary" }, "Who can see these notes"),
      row("success", "Shared", "Every Claude chat in this workspace, including new ones"),
      row("neutral", "Not yet", "Codex, Pi, OpenCode and Antigravity chats. They don't use these notes."),
      row("neutral", "Not shared", "Your other workspaces. Each has its own notes."),
      row(document.backups.is_backed_up ? "warning" : "neutral", "Backups", backupsLine(document.backups)),
      m(
        "p",
        { class: "m-0 type-helper text-secondary" },
        "What you say inside a chat stays in that chat unless it writes a note.",
      ),
      disclose("how", "How this works", "Hide how this works"),
      openDetails.has("how")
        ? m("div", { class: DETAILS_CLASS }, [
            m(
              "p",
              { class: "m-0" },
              `Notes live in ${document.notes_dir}/, one Markdown file each (autoMemoryDirectory in .claude/settings.json).`,
            ),
            m(
              "p",
              { class: "m-0 mt-2" },
              `${document.index_path} lists them all. Every Claude chat reads that list when it starts and opens a note when it looks relevant, so a change reaches a chat when it next starts.`,
            ),
            m(
              "p",
              { class: "m-0 mt-2" },
              "Who wrote a note and who read it come from the chats' own transcripts: each Write, Edit or Read of a note's file.",
            ),
            m(
              "p",
              { class: "m-0 mt-2" },
              `Deleting a note erases its file and removes its line from ${document.index_path}. Nothing in the workspace keeps a copy.`,
            ),
            m("p", { class: "m-0 mt-2" }, backupsDetail(document.backups)),
            m(
              "p",
              { class: "m-0 mt-2" },
              "Every agent also follows the workspace's instructions (AGENTS.md, and CLAUDE.md for Claude) and its skills. Those aren't notes and aren't shown here.",
            ),
          ])
        : null,
    ]);
  }

  function editor(note: Note, attrs: MemoriesPageAttrs, current: Draft): m.Vnode {
    const descriptionId = `edit-description-${note.file_name}`;
    const bodyId = `edit-body-${note.file_name}`;
    return m(
      "div",
      {
        key: note.file_name,
        class: "memories-note flex flex-col gap-3 rounded-lg border border-strong bg-surface p-4",
      },
      [
        m("label", { class: "flex flex-col gap-1 type-helper text-secondary", for: descriptionId }, [
          "Summary",
          m("input", {
            id: descriptionId,
            class: inputClass(),
            value: current.description,
            oninput: (event: InputEvent) => (current.description = (event.target as HTMLInputElement).value),
          }),
        ]),
        m("label", { class: "flex flex-col gap-1 type-helper text-secondary", for: bodyId }, [
          "Details",
          m("textarea", {
            id: bodyId,
            class: `${inputClass()} min-h-28`,
            value: current.body,
            oninput: (event: InputEvent) => (current.body = (event.target as HTMLTextAreaElement).value),
          }),
        ]),
        m("div", { class: "flex flex-wrap items-center justify-between gap-3" }, [
          m(
            "span",
            { class: "type-helper text-secondary" },
            "New chats use the new version. Open chats pick it up when they restart.",
          ),
          m("div", { class: "flex gap-2" }, [
            m(Button, { variant: "secondary", sm: true, disabled: isBusy, onclick: () => (draft = null) }, "Cancel"),
            m(
              Button,
              {
                variant: "primary",
                sm: true,
                disabled: isBusy || current.description.trim() === "",
                onclick: () =>
                  run(async () => {
                    const error = await attrs.onSave(note, current.description, current.body);
                    if (error === null) draft = null;
                    return error;
                  }, "Saved. New chats will use this version."),
              },
              "Save",
            ),
          ]),
        ]),
      ],
    );
  }

  function noteCard(note: Note, attrs: MemoriesPageAttrs): m.Vnode {
    if (draft !== null && draft.fileName === note.file_name) return editor(note, attrs, draft);
    const fileKey = `file:${note.file_name}`;
    return m(
      "article",
      {
        key: note.file_name,
        class: "memories-note flex flex-col gap-2 rounded-lg border border-default bg-surface p-4",
      },
      [
        m("h4", { class: "m-0 type-body font-semibold text-primary text-pretty" }, note.description),
        ...bodyBlocks(note.body).map((block) =>
          block.label === null
            ? m("p", { class: "m-0 max-w-[65ch] type-body text-primary" }, block.text)
            : m("p", { class: "m-0 max-w-[65ch] type-body text-primary" }, [
                m("span", { class: "font-semibold" }, `${block.label}: `),
                block.text,
              ]),
        ),
        m("div", { class: "flex flex-wrap items-center justify-between gap-2 pt-1" }, [
          m("span", { class: "flex flex-wrap items-center gap-x-1.5 type-helper text-secondary" }, [
            `${attributionLine(note.attribution)} · updated ${formatDate(note.modified_at)} ·`,
            disclose(fileKey, "Show file", "Hide file"),
          ]),
          m("div", { class: "flex gap-2" }, [
            m(
              Button,
              {
                variant: "secondary",
                sm: true,
                disabled: isBusy,
                onclick: () => (draft = { fileName: note.file_name, description: note.description, body: note.body }),
              },
              "Edit",
            ),
            m(
              Button,
              {
                variant: "ghost-destructive",
                sm: true,
                disabled: isBusy,
                onclick: () => (pendingDelete = note),
              },
              "Delete",
            ),
          ]),
        ]),
        openDetails.has(fileKey)
          ? m("div", { class: DETAILS_CLASS }, [
              m("div", { class: "pb-2 text-faint" }, note.file_name),
              m("pre", { class: "m-0 whitespace-pre-wrap break-words" }, note.raw_text),
            ])
          : null,
      ],
    );
  }

  function deleteDialog(note: Note, backups: BackupRetention, attrs: MemoriesPageAttrs): m.Children {
    const close = (): void => {
      pendingDelete = null;
    };
    return m(NoticeDialog, {
      title: "Delete this note for good?",
      body: [
        `"${note.description}"`,
        "It's erased from this workspace, and new chats won't see it. This can't be undone.",
        deleteBackupsWarning(backups),
        "Chats that are open now may still remember it until they restart, and the chat that wrote it still shows it in its history.",
      ],
      dismissLabel: "Cancel",
      isDismissable: !isBusy,
      onDismiss: close,
      actions: [
        {
          label: "Delete for good",
          tooltip: "Erases the note's file and its line in the list every chat reads",
          isDestructive: true,
          isDisabled: isBusy,
          run: () =>
            void run(async () => {
              const error = await attrs.onDelete(note);
              close();
              return error;
            }, deletedStatus(backups)),
        },
      ],
    });
  }

  return {
    view: ({ attrs }) => {
      const { state } = attrs;
      if (state.kind === "loading") return m("p", { class: "m-0 type-body text-secondary" }, "Reading the notes…");
      if (state.kind === "failed")
        return m("p", { class: "m-0 type-body text-danger" }, `Couldn't read the notes. ${state.message}`);
      const document = state.document;
      const count = document.notes.length;
      return m("div", { class: "flex flex-col gap-6" }, [
        m("div", { class: "flex flex-col gap-2" }, [
          m("span", { class: `self-start ${badgeClass("accent")}` }, "Claude chats"),
          m(
            "h2",
            { class: "m-0 type-heading-lg text-primary text-balance" },
            count === 0
              ? "Your Claude chats haven't written anything down yet."
              : `Your Claude chats remember ${countLabel(count, "thing", "things")} about you and your work.`,
          ),
          m(
            "p",
            { class: "m-0 type-body text-secondary" },
            count === 0
              ? "When a chat learns something worth keeping, like how you like things done or what you're working on, it writes a note here. You'll be able to read, correct or remove each one."
              : "They read these notes at the start of every chat. Fix anything that's wrong, or delete it.",
          ),
        ]),
        whoCanSee(document),
        statusMessage === null ? null : m("p", { class: "m-0 type-body text-primary", role: "status" }, statusMessage),
        GROUPS.map((group) => ({ group, notes: document.notes.filter((note) => note.note_type === group.type) }))
          .filter(({ notes }) => notes.length > 0)
          .map(({ group, notes }) =>
            m("section", { key: group.type, class: "flex flex-col gap-2" }, [
              m(
                "h3",
                { class: `m-0 border-b border-default pb-1.5 ${SECTION_HEADING_CLASS}` },
                `${group.label} · ${notes.length}`,
              ),
              notes.map((note) => noteCard(note, attrs)),
            ]),
          ),
        pendingDelete === null ? null : deleteDialog(pendingDelete, document.backups, attrs),
        document.messages.length === 0
          ? null
          : m(
              "div",
              { class: DETAILS_CLASS },
              document.messages.map((message) => m("div", message)),
            ),
      ]);
    },
  };
}
