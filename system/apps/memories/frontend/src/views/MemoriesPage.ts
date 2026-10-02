/**
 * The page, top to bottom: how many things the chats remember, when a chat saves a note and what it never saves,
 * and how much of the list every chat starts with it loads; where the notes go (stored here, read by every Claude
 * and pi chat, sent to a chat's AI provider when used, kept in backups, not shared with other workspaces); then the
 * notes grouped by what they are about, each group saying what belongs in it (and what to try when it is empty),
 * each note with the line chats start with, who wrote it, how many chats read it, the file as it is on disk, and
 * Edit and Delete. Delete asks first, saying what still holds a copy afterwards.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { badgeClass } from "@imbue/workspace-ui/src/components/Badge";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { makeNoticeDialog } from "@imbue/workspace-ui/src/components/NoticeDialog";
import type { BackupRetention, Note, NotesDocument, NotesState, NoteType } from "../models/notes";
import {
  backupsDetail,
  backupsLine,
  bodyBlocks,
  countLabel,
  deleteBackupsWarning,
  deletedStatus,
  attentionLine,
  backupsChip,
  formatDate,
  indexStatus,
  indexSummaryLine,
  writerLine,
} from "./format";

const SECTION_HEADING_CLASS = "type-section text-faint";
const DETAILS_CLASS =
  "memories-details overflow-x-auto rounded-md bg-surface-secondary p-3 font-mono type-helper text-secondary";
const DISCLOSE_CLASS = "memories-disclose self-start cursor-pointer type-helper text-accent hover:underline";
const NoticeDialog = makeNoticeDialog();

interface Group {
  readonly type: NoteType;
  readonly label: string;
  readonly description: string;
  /** Something to tell a chat that would land a note here; null for the group that only shows when it has notes. */
  readonly example: string | null;
}

// The four kinds Claude Code's memory (and the protocol pi follows) sorts a note into, in the user's words.
const GROUPS: readonly Group[] = [
  {
    type: "USER",
    label: "About you",
    description: "Who you are: your role, what you know well, where you are.",
    example: "Remember that I'm a teacher and new to coding.",
  },
  {
    type: "FEEDBACK",
    label: "How you like things done",
    description: "Corrections and preferences for how chats work with you. Chats save these most often.",
    example: "From now on, keep your answers short.",
  },
  {
    type: "PROJECT",
    label: "What you're working on",
    description: "Goals, deadlines and decisions that aren't written down in your files.",
    example: "Remember that the launch moved to March 3.",
  },
  {
    type: "REFERENCE",
    label: "Where things are",
    description: "Where to look for things: links, dashboards, documents.",
    example: "Remember that our team's notes are in the Design wiki.",
  },
  { type: "OTHER", label: "Other notes", description: "Notes that don't say what kind they are.", example: null },
];

const STATUS_CLASS: Readonly<Record<"seen" | "not-listed" | "past-limit", string>> = {
  seen: "type-helper text-secondary",
  "not-listed": "type-helper text-warning",
  "past-limit": "type-helper text-warning",
};

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

  /** The few facts worth seeing every time, and the way into the rest. */
  function memoryFacts(document: NotesDocument): m.Vnode {
    const backups = backupsChip(document.backups);
    return m("div", { class: "memories-facts flex flex-wrap items-center gap-2" }, [
      m("span", { class: badgeClass("success") }, "Shared with your Claude and pi chats"),
      m("span", { class: badgeClass("neutral") }, "Stays in this workspace"),
      m("span", { class: badgeClass(backups.isWarning ? "warning" : "neutral") }, backups.text),
      disclose("how", "How memory works", "Hide how memory works"),
    ]);
  }

  /** Everything behind "How memory works": what is saved, what chats use, where notes go, and how it's built. */
  function howMemoryWorks(document: NotesDocument): m.Vnode {
    const row = (tone: "success" | "neutral" | "warning", label: string, text: string): m.Vnode =>
      m("div", { class: "grid grid-cols-[96px_minmax(0,1fr)] items-baseline gap-3" }, [
        m("span", { class: `justify-self-start ${badgeClass(tone)}` }, label),
        m("span", { class: "type-body text-primary" }, text),
      ]);
    const heading = (text: string): m.Vnode => m("h3", { class: "m-0 type-label text-primary" }, text);
    const summary = indexSummaryLine(document.index, document.notes.length);
    return m(
      "section",
      { class: "memories-how flex flex-col gap-4 rounded-lg border border-default bg-surface-secondary p-4" },
      [
        m("div", { class: "flex flex-col gap-1" }, [
          heading("What gets saved"),
          m(
            "p",
            { class: "m-0 type-body text-secondary" },
            "A chat saves a note when you ask it to remember something, when you correct how it works, or when you mention something that will still matter later. It never saves passwords or keys, sensitive personal details unless you ask, or what's already in your files.",
          ),
        ]),
        m("div", { class: "flex flex-col gap-1" }, [
          heading("What chats use"),
          m(
            "p",
            { class: "m-0 type-body text-secondary" },
            summary ?? "Nothing is saved yet, so chats start with an empty list.",
          ),
        ]),
        m("div", { class: "flex flex-col gap-2" }, [
          heading("Where your notes go"),
          row("neutral", "Stored", "In this workspace only. They aren't synced to GitHub."),
          row(
            "success",
            "Read by",
            "Every Claude and pi chat in this workspace, including new ones and the background tasks they start.",
          ),
          row("neutral", "Not yet", "Codex, OpenCode and Antigravity chats. They don't use these notes."),
          row(
            "neutral",
            "Sent to",
            "The AI provider of a chat that uses a note (Anthropic, for Claude chats), as part of that chat.",
          ),
          row("neutral", "Not shared", "Your other workspaces. Each has its own notes."),
          row(document.backups.is_backed_up ? "warning" : "neutral", "Backups", backupsLine(document.backups)),
        ]),
        disclose("tech", "Technical details", "Hide technical details"),
        openDetails.has("tech")
          ? m("div", { class: DETAILS_CLASS }, [
              m(
                "p",
                { class: "m-0" },
                `Notes live in ${document.notes_dir}/, one Markdown file each (autoMemoryDirectory in .claude/settings.json).`,
              ),
              m(
                "p",
                { class: "m-0 mt-2" },
                `${document.index_path} lists them all, one line each. Every Claude chat loads that list when it starts (the first ${document.index.max_lines} lines, or ${Math.round(document.index.max_bytes / 1024)}KB, whichever is less) and opens a note when its line looks relevant; a note saved later reaches an open Claude chat on its next message.`,
              ),
              m(
                "p",
                { class: "m-0 mt-2" },
                "pi chats get the same list, and how to keep it, from .pi/extensions/memory.ts (system/scripts/agent_memory_context.py), which also stamps each note a pi chat saves with its source and time; a change reaches them on their next message.",
              ),
              m(
                "p",
                { class: "m-0 mt-2" },
                "Which chat wrote a note and who read it come from the chats' own transcripts, Claude's and pi's: each write, edit or read of a note's file.",
              ),
              m(
                "p",
                { class: "m-0 mt-2" },
                `Deleting a note erases its file and removes its line from ${document.index_path}. Nothing in the workspace keeps a copy.`,
              ),
              m(
                "p",
                { class: "m-0 mt-2" },
                "Each delete or edit made here is recorded (the note's file name and when, never what it said) in data/.state/memories/user-changes.jsonl for 30 days. Every chat reads that record before each message, so one that still remembers the note in its conversation doesn't save it again.",
              ),
              m("p", { class: "m-0 mt-2" }, backupsDetail(document.backups)),
              m(
                "p",
                { class: "m-0 mt-2" },
                "Every agent also follows the workspace's instructions (AGENTS.md, and CLAUDE.md for Claude) and its skills. Those aren't notes and aren't shown here.",
              ),
            ])
          : null,
      ],
    );
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
            "New chats use the new version. Open chats are told you changed it on their next message.",
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

  function indexStatusLine(note: Note, maxIndexLines: number): m.Vnode {
    const status = indexStatus(note, maxIndexLines);
    return m(
      "p",
      { class: `memories-index-status m-0 ${STATUS_CLASS[status.kind]}`, "data-status": status.kind },
      status.text,
    );
  }

  function noteCard(note: Note, attrs: MemoriesPageAttrs, maxIndexLines: number): m.Vnode {
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
        indexStatusLine(note, maxIndexLines),
        m("div", { class: "flex flex-wrap items-center justify-between gap-2 pt-1" }, [
          m("span", { class: "flex flex-wrap items-center gap-x-1.5 type-helper text-secondary" }, [
            `${writerLine(note)} · updated ${formatDate(note.modified_at)} ·`,
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
        "Chats that are open now still have it in their conversation. They're told you deleted it and asked not to save it again, and the chat that wrote it still shows it in its history.",
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
      const attention = attentionLine(document.notes, document.index.max_lines);
      return m("div", { class: "flex flex-col gap-6" }, [
        m("div", { class: "flex flex-col gap-3" }, [
          m(
            "h2",
            { class: "m-0 type-heading-lg text-primary text-balance" },
            count === 0
              ? "Your chats haven't written anything down yet."
              : `Your chats remember ${countLabel(count, "thing", "things")} about you and your work.`,
          ),
          m(
            "p",
            { class: "m-0 type-body text-secondary" },
            "Chats save a note when you ask them to remember something or correct how they work. Fix or delete anything that's wrong.",
          ),
          memoryFacts(document),
          openDetails.has("how") ? howMemoryWorks(document) : null,
          attention === null
            ? null
            : m("p", { class: "memories-attention m-0 type-body text-warning", role: "note" }, attention),
        ]),
        statusMessage === null ? null : m("p", { class: "m-0 type-body text-primary", role: "status" }, statusMessage),
        GROUPS.map((group) => ({ group, notes: document.notes.filter((note) => note.note_type === group.type) }))
          .filter(({ group, notes }) => notes.length > 0 || group.example !== null)
          .map(({ group, notes }) =>
            m("section", { key: group.type, class: "memories-group flex flex-col gap-2", "data-group": group.type }, [
              m("div", { class: "flex flex-col gap-0.5 border-b border-default pb-1.5" }, [
                m("h3", { class: `m-0 ${SECTION_HEADING_CLASS}` }, `${group.label} · ${notes.length}`),
                m("p", { class: "m-0 type-helper text-secondary" }, group.description),
              ]),
              notes.length === 0 && group.example !== null
                ? m(
                    "p",
                    { class: "m-0 type-helper text-faint" },
                    `Nothing yet. Try telling a chat: “${group.example}”`,
                  )
                : notes.map((note) => noteCard(note, attrs, document.index.max_lines)),
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
