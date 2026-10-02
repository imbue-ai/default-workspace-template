/**
 * The page, top to bottom: how many things the chats remember, in one sentence; three facts (which chats use the
 * notes, not shared with other workspaces, what the backups keep), "How memory works", which opens what gets saved,
 * what chats use, where the notes go and the technical details, and "Settings", which opens the switches that pause
 * memory or turn it off for one kind of chat; a warning only when something in the list chats load is wrong; then the
 * notes grouped by what they are about, each group saying what belongs in it (and what to try when it is empty), each
 * note a short card whose full text and file open on "Show more", with Edit and Delete. Delete asks first, saying
 * what still holds a copy afterwards.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { badgeClass } from "@imbue/workspace-ui/src/components/Badge";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { makeNoticeDialog } from "@imbue/workspace-ui/src/components/NoticeDialog";
import type {
  BackupRetention,
  MemoryControls,
  MemoryHarness,
  Note,
  NotesDocument,
  NotesState,
  NoteType,
} from "../models/notes";
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
  HARNESS_LABEL,
  indexWarning,
  MEMORY_HARNESSES,
  readersLine,
  sharingChip,
  indexSummaryLine,
  writerLine,
} from "./format";

const SECTION_HEADING_CLASS = "type-section text-secondary";
const CONTROLS_SAVED =
  "Saved. pi chats follow it from their next message, and new Claude chats right away. Open Claude chats are told on their next message.";
const DETAILS_CLASS =
  "memories-details overflow-x-auto rounded-md bg-surface-secondary p-3 font-mono type-helper text-secondary";
const DISCLOSE_CLASS = "memories-disclose self-start cursor-pointer type-helper text-accent hover:underline";
const NoticeDialog = makeNoticeDialog();
// The shared warning badge sets amber text on an amber fill (4.0:1, under WCAG AA's 4.5:1); the caution class in
// style.css keeps the fill and darkens the text.
const CAUTION_BADGE_CLASS = badgeClass("warning", { extra: "memories-caution" });

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

export interface MemoriesPageAttrs {
  readonly state: NotesState;
  readonly onSave: (fileName: string, description: string, body: string, version: string) => Promise<string | null>;
  readonly onDelete: (note: Note) => Promise<string | null>;
  readonly onSaveControls: (controls: MemoryControls) => Promise<string | null>;
}

/** An edit in progress, and the version of the note it started from: the page re-reads the notes whenever its
 *  window regains focus, so the note it renders may be newer than the one being edited. */
interface Draft {
  readonly fileName: string;
  readonly version: string;
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
    const sharing = sharingChip(document.controls);
    return m("div", { class: "memories-facts flex flex-wrap items-center gap-2" }, [
      m(
        "span",
        { class: `memories-sharing ${sharing.isOff ? CAUTION_BADGE_CLASS : badgeClass("accent")}` },
        sharing.text,
      ),
      m("span", { class: badgeClass("neutral") }, "Not shared with other workspaces"),
      m("span", { class: backups.isWarning ? CAUTION_BADGE_CLASS : badgeClass("neutral") }, backups.text),
      disclose("how", "How memory works", "Hide how memory works"),
      disclose("settings", "Settings", "Hide settings"),
    ]);
  }

  function memorySwitch(
    id: string,
    label: string,
    hint: string | null,
    isOn: boolean,
    isEnabled: boolean,
    onToggle: () => void,
  ): m.Vnode {
    return m("div", { class: "flex items-start gap-3" }, [
      m(
        "button",
        {
          type: "button",
          id,
          role: "switch",
          "aria-checked": String(isOn),
          disabled: isBusy || !isEnabled,
          class: `memories-switch relative mt-0.5 inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${isOn ? "border-accent bg-accent" : "border-strong bg-surface"}`,
          onclick: onToggle,
        },
        m("span", {
          class: `inline-block h-3.5 w-3.5 rounded-full shadow transition-transform ${isOn ? "translate-x-[18px] bg-surface" : "translate-x-[2px] border border-strong bg-surface-secondary"}`,
        }),
      ),
      m("label", { class: "flex flex-col gap-0.5", for: id }, [
        m("span", { class: "type-body text-primary" }, label),
        hint === null ? null : m("span", { class: "type-helper text-secondary" }, hint),
      ]),
    ]);
  }

  /** The switches behind "Settings": pause memory for every chat, or turn it off for one kind of chat. */
  function memorySettings(document: NotesDocument, attrs: MemoriesPageAttrs): m.Vnode {
    // Unreadable settings are shown as chats read them: memory off.
    const controls: MemoryControls = document.controls ?? { is_paused: true, disabled_harnesses: [] };
    const save = (next: MemoryControls): void => void run(() => attrs.onSaveControls(next), CONTROLS_SAVED);
    const toggleHarness = (harness: MemoryHarness): void =>
      save({
        is_paused: controls.is_paused,
        disabled_harnesses: controls.disabled_harnesses.includes(harness)
          ? controls.disabled_harnesses.filter((disabled) => disabled !== harness)
          : [...controls.disabled_harnesses, harness],
      });
    return m(
      "section",
      { class: "memories-settings flex flex-col gap-3 rounded-lg border border-default bg-surface-secondary p-4" },
      [
        document.controls === null
          ? m(
              "p",
              { class: "m-0 type-body text-warning", role: "note" },
              "The memory settings couldn't be read, so chats treat memory as off. Choosing a setting here replaces them.",
            )
          : null,
        memorySwitch(
          "memory-switch-all",
          "Use memory",
          "Chats keep what's saved, but while this is off they don't use it or save anything new.",
          !controls.is_paused,
          true,
          () => save({ is_paused: !controls.is_paused, disabled_harnesses: controls.disabled_harnesses }),
        ),
        m(
          "div",
          { class: "flex flex-wrap gap-x-8 gap-y-2 pl-12" },
          MEMORY_HARNESSES.map((harness) =>
            memorySwitch(
              `memory-switch-${harness.toLowerCase()}`,
              `${HARNESS_LABEL[harness]} chats`,
              null,
              !controls.disabled_harnesses.includes(harness),
              !controls.is_paused,
              () => toggleHarness(harness),
            ),
          ),
        ),
        m(
          "p",
          { class: "m-0 type-helper text-secondary" },
          "pi chats follow these from their next message. New Claude chats follow them right away; open Claude chats are told on their next message, and only pick memory back up once restarted.",
        ),
      ],
    );
  }

  /** Everything behind "How memory works": what is saved, what chats use, where notes go, and how it's built. */
  function howMemoryWorks(document: NotesDocument): m.Vnode {
    const row = (tone: "accent" | "neutral" | "caution", label: string, text: string): m.Vnode =>
      m("div", { class: "grid grid-cols-[96px_minmax(0,1fr)] items-baseline gap-3" }, [
        m(
          "span",
          { class: `justify-self-start ${tone === "caution" ? CAUTION_BADGE_CLASS : badgeClass(tone)}` },
          label,
        ),
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
            "Chats are told to save a note when you ask them to remember something, when you correct how they work, or when you mention something that will still matter later, and never to save passwords or keys, sensitive personal details unless you ask, or what's already in your files.",
          ),
        ]),
        m("div", { class: "flex flex-col gap-1" }, [
          heading("What chats use"),
          m(
            "p",
            { class: "m-0 type-body text-secondary" },
            summary ??
              (document.index.line_count === 0
                ? "Nothing is saved yet, so chats start with an empty list."
                : `Nothing is saved, but the list chats start with still has ${countLabel(document.index.line_count, "line", "lines")}.`),
          ),
        ]),
        m("div", { class: "flex flex-col gap-2" }, [
          heading("Where your notes go"),
          row("neutral", "Stored", "In this workspace, and in its backups. They aren't synced to GitHub."),
          row("accent", "Read by", readersLine(document.controls)),
          row("neutral", "Not yet", "Codex, OpenCode and Antigravity chats. They don't use these notes."),
          row(
            "neutral",
            "Sent to",
            "Each chat's AI provider (Anthropic, for Claude chats): the list of summaries with every chat, and a note's full text when a chat opens it.",
          ),
          row("neutral", "Not shared", "Your other workspaces. Each has its own notes."),
          row(document.backups.is_backed_up ? "caution" : "neutral", "Backups", backupsLine(document.backups)),
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
                `${document.index_path} lists them all, one line each, and each line is kept in step with its note's summary whenever a chat changes a note. Every Claude chat loads that list when it starts (the first ${document.index.max_lines} lines, or ${Math.round(document.index.max_bytes / 1024)}KB, whichever is less) and opens a note when its line looks relevant; a note saved later reaches an open Claude chat on its next message.`,
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
                "Each delete or edit made here is recorded (the note's file name and when, never what it said) in data/.apps/memories/user-changes.jsonl for 30 days. Every chat reads that record before each message, so one that still remembers the note in its conversation doesn't save it again.",
              ),
              m(
                "p",
                { class: "m-0 mt-2" },
                "The switches under Settings are kept in data/.apps/memories/settings.json, which system/scripts/agent_memory_context.py reads before every pi and Claude message. Turning Claude's memory off also sets autoMemoryEnabled to false in .claude/settings.local.json, which Claude Code reads when a chat starts.",
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
    const isConflicted = current.version !== note.version;
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
        isConflicted
          ? m(
              "div",
              { class: "memories-conflict flex flex-col gap-1 rounded-md bg-surface-secondary p-3", role: "note" },
              [
                m(
                  "p",
                  { class: "m-0 type-body text-primary" },
                  "A chat changed this note while you were editing it. Its version now reads:",
                ),
                m("p", { class: "m-0 type-body font-semibold text-primary" }, note.description),
                m("p", { class: "m-0 whitespace-pre-wrap type-body text-primary" }, note.body),
                m("p", { class: "m-0 type-helper text-secondary" }, "Replacing it keeps only your version."),
              ],
            )
          : null,
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
                    // A replace is the user's explicit choice after seeing the chat's version, so it is made against
                    // that version; a plain save is made against the version the edit started from.
                    const version = isConflicted ? note.version : current.version;
                    const error = await attrs.onSave(note.file_name, current.description, current.body, version);
                    if (error === null) draft = null;
                    return error;
                  }, "Saved. New chats will use this version."),
              },
              isConflicted ? "Replace with my version" : "Save",
            ),
          ]),
        ]),
      ],
    );
  }

  function noteText(note: Note): m.Vnode[] {
    return bodyBlocks(note.body).map((block) =>
      block.label === null
        ? m("p", { class: "m-0 max-w-[65ch] type-body text-primary" }, block.text)
        : m("p", { class: "m-0 max-w-[65ch] type-body text-primary" }, [
            m("span", { class: "font-semibold" }, `${block.label}: `),
            block.text,
          ]),
    );
  }

  /** A note's summary and who saved it; its full text, and the file behind it, only when asked for. */
  function noteCard(note: Note, attrs: MemoriesPageAttrs): m.Vnode {
    if (draft !== null && draft.fileName === note.file_name) return editor(note, attrs, draft);
    const moreKey = `more:${note.file_name}`;
    const fileKey = `file:${note.file_name}`;
    const isExpanded = openDetails.has(moreKey);
    const warning = indexWarning(note);
    return m(
      "article",
      {
        key: note.file_name,
        class: "memories-note flex flex-col gap-2 rounded-lg border border-default bg-surface p-4",
        "data-expanded": String(isExpanded),
      },
      [
        m("h4", { class: "m-0 type-body font-semibold text-primary text-pretty" }, note.description),
        warning === null ? null : m("p", { class: "memories-index-warning m-0 type-helper text-warning" }, warning),
        ...(isExpanded ? noteText(note) : []),
        m("div", { class: "flex flex-wrap items-center justify-between gap-2 pt-1" }, [
          m("span", { class: "flex flex-wrap items-center gap-x-1.5 type-helper text-secondary" }, [
            `${writerLine(note)} · updated ${formatDate(note.modified_at)} ·`,
            disclose(moreKey, "Show more", "Show less"),
            ...(isExpanded
              ? [m("span", { "aria-hidden": "true" }, "·"), disclose(fileKey, "Show file", "Hide file")]
              : []),
          ]),
          m("div", { class: "flex gap-2" }, [
            m(
              Button,
              {
                variant: "secondary",
                sm: true,
                disabled: isBusy,
                onclick: () =>
                  (draft = {
                    fileName: note.file_name,
                    version: note.version,
                    description: note.description,
                    body: note.body,
                  }),
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
        isExpanded && openDetails.has(fileKey)
          ? m("div", { class: DETAILS_CLASS }, [
              m("div", { class: "pb-2 text-secondary" }, note.file_name),
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
      const attention = attentionLine(document.notes, document.index.missing_files);
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
          openDetails.has("settings") ? memorySettings(document, attrs) : null,
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
                    { class: "m-0 type-helper text-secondary" },
                    `Nothing yet. Try telling a chat: “${group.example}”`,
                  )
                : notes.map((note) => noteCard(note, attrs)),
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
