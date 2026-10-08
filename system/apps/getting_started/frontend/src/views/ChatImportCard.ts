/**
 * "Bring in your chats": the card at the top of the page offering to copy the user's Claude and
 * ChatGPT conversations into the workspace. Its action starts a chat whose first message asks for
 * the import (the import-chats skill matches on it); the agent connects each account and runs the
 * import, and the card follows along from the skill's status: counting up while it runs, then the
 * result, or a way back in when a source needs the user. "Not now" and "Hide" put it away for good.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import type { ChatImport, ChatImportSource } from "../models/ChatImport";
import { SOURCE_LABELS, cardPhase, importedSummary, sourceNames } from "../models/ChatImport";

export const IMPORT_PROMPT =
  "Bring my Claude and ChatGPT chats into this workspace, so you can search and build on my past conversations.";

/** The later prompts name only the sources the card is about: a source the user declined is never
 *  recorded, and naming it would have the agent ask to connect it again. */
export function updatePrompt(sources: Record<string, ChatImportSource>): string {
  return `Check my ${sourceNames(Object.keys(sources))} chats for new conversations and bring them into this workspace.`;
}

export function resumePrompt(sources: Record<string, ChatImportSource>): string {
  const unfinished = Object.keys(sources).filter((key) => sources[key].state !== "imported");
  return `My chat import did not finish. Pick it up and bring the rest of my ${sourceNames(unfinished)} chats into this workspace.`;
}

export interface ChatImportCardAttrs {
  readonly chatImport: ChatImport | null;
  /** Start a chat whose first message is ``text``. */
  readonly onStartWithText: (text: string) => void;
  readonly onDismiss: () => void;
}

/** A running import's fetched and to-fetch counts, when the source reports a total. */
function fetchProgress(source: ChatImportSource): { fetched: number; total: number } | null {
  if (source.state !== "importing" || typeof source.to_fetch !== "number" || typeof source.fetched !== "number") {
    return null;
  }
  return { fetched: source.fetched, total: source.to_fetch };
}

function sourceLine(label: string, source: ChatImportSource): string {
  const progress = fetchProgress(source);
  switch (source.state) {
    case "importing":
      return progress === null
        ? `${label}: ${source.conversations.toLocaleString()} so far`
        : `${label}: ${progress.fetched.toLocaleString()} of ${progress.total.toLocaleString()}`;
    case "imported":
      return `${label}: ${source.conversations.toLocaleString()} imported`;
    case "needs_sign_in":
      return `${label}: needs you to sign in again`;
    case "failed":
      return `${label}: did not finish`;
  }
}

function sourceLines(sources: Record<string, ChatImportSource>): m.Vnode {
  return m(
    "ul",
    { class: "chat-import-sources mt-2 type-helper text-secondary" },
    SOURCE_LABELS.flatMap(([key, label]) => {
      const source = sources[key];
      if (source === undefined) return [];
      const progress = fetchProgress(source);
      return [
        m("li", { key, "data-chat-import-source": key }, [
          sourceLine(label, source),
          progress === null || progress.total === 0
            ? null
            : m(
                "div",
                { class: "mt-1 h-1 max-w-xs overflow-hidden rounded-full bg-fill-hover", "data-chat-import-bar": key },
                m("div", {
                  class: "h-full rounded-full bg-accent",
                  style: `width: ${Math.min(100, (100 * progress.fetched) / progress.total).toFixed(1)}%`,
                }),
              ),
        ]),
      ];
    }),
  );
}

/** The ghost button that puts the card away, labelled for the phase it closes. */
function dismissButton(onDismiss: () => void, label: string): m.Vnode {
  return m(Button, { variant: "ghost", sm: true, "data-chat-import-action": "dismiss", onclick: onDismiss }, label);
}

export const ChatImportCard: m.Component<ChatImportCardAttrs> = {
  view({ attrs }) {
    const phase = cardPhase(attrs.chatImport);
    if (phase === "hidden" || attrs.chatImport === null) return null;
    const sources = attrs.chatImport.sources;
    const start = (text: string) => () => attrs.onStartWithText(text);
    let title: string;
    let body: m.Children;
    let actions: m.Children;
    switch (phase) {
      case "offer":
        title = "Bring in your chats";
        body = m(
          "p",
          { class: "type-helper mt-1 text-secondary" },
          "Copy your Claude and ChatGPT conversations here, so this workspace can search and build on " +
            "what you have already worked through. You sign in once; the rest happens on its own.",
        );
        actions = [
          m(
            Button,
            { variant: "primary", sm: true, "data-chat-import-action": "import", onclick: start(IMPORT_PROMPT) },
            "Import my chats",
          ),
          dismissButton(attrs.onDismiss, "Not now"),
        ];
        break;
      case "importing":
        title = "Bringing in your chats…";
        body = sourceLines(sources);
        actions = null;
        break;
      case "attention":
        title = "Your chat import needs a hand";
        body = sourceLines(sources);
        actions = [
          m(
            Button,
            {
              variant: "primary",
              sm: true,
              "data-chat-import-action": "resume",
              onclick: start(resumePrompt(sources)),
            },
            "Finish importing",
          ),
          dismissButton(attrs.onDismiss, "Hide"),
        ];
        break;
      case "imported":
        title = "Your chats are here";
        body = m("p", { class: "type-helper mt-1 text-secondary" }, `${importedSummary(sources)}.`);
        actions = [
          m(
            Button,
            {
              variant: "secondary",
              sm: true,
              "data-chat-import-action": "update",
              onclick: start(updatePrompt(sources)),
            },
            "Check for new chats",
          ),
          dismissButton(attrs.onDismiss, "Hide"),
        ];
        break;
    }
    return m(
      "section",
      {
        "data-section": "chat-import",
        "data-chat-import-phase": phase,
        class: "getting-started-chat-import mt-6 rounded-xl border border-default bg-surface p-4 text-primary",
      },
      [
        m("h2", { class: "type-label" }, title),
        body,
        actions === null ? null : m("div", { class: "mt-3 flex flex-wrap gap-2" }, actions),
      ],
    );
  },
};
