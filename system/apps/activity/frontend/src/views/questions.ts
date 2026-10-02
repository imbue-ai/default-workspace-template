/**
 * The questions the memory tab answers in place, picked for what the page is showing, and the message it drafts
 * into the user's chat when an answer is not enough. Pure, so each is tested on its own.
 *
 * The drafted message carries the numbers on screen and names System Monitor, so the agent answering can read the same figures (and its README) rather than guess.
 */

import type { ActivityItem, ActivitySummary } from "../models/summary";
import { formatBytes, formatKib, headlineFor } from "./format";

export interface Question {
  readonly id: string;
  readonly question: string;
  readonly answer: readonly string[];
}

const FILLS_UP: Question = {
  id: "fills-up",
  question: "What happens if it fills up?",
  answer: [
    'The workspace closes something to make room, starting with what\'s marked "likely closed first": usually a browser tab, a test an agent is running, or an idle chat.',
    "A closed chat keeps its conversation and picks up when you message it. Work that was in progress at that moment may need to be redone.",
  ],
};

const FREE_UP: Question = {
  id: "free-up",
  question: "How do I free up memory?",
  answer: [
    "Stop chats you aren't using. Their conversation is kept, and they pick up where they left off when you message them.",
    "Some apps, like this one, stop by themselves a minute after you close their windows.",
  ],
};

const MORE_MEMORY: Question = {
  id: "more-memory",
  question: "Can I get more memory?",
  answer: [
    "Not from inside the workspace: its memory is set when the workspace is created.",
    'For a workspace on Imbue Cloud, Imbue Studio shows its size in the workspace\'s settings under "Machine size". A larger size, if one is arranged, applies after a restart.',
  ],
};

const WHY_FIRST: Question = {
  id: "why-first",
  question: "Why would that one be closed first?",
  answer: [
    "Whichever closes things first, the memory guard or the system at the memory limit, weighs how much each thing uses against how expendable it's marked. Browser tabs and agents' test runs are marked most expendable, then idle chats, then chats that are working. The services the workspace needs come last.",
    "It's a prediction: what's marked can change as things start, finish, or grow.",
  ],
};

function largestItems(summary: ActivitySummary): ActivityItem[] {
  return [...summary.chats, ...summary.apps, ...summary.services]
    .filter((item) => item.rss_kib > 0 && item.kind !== "PLUMBING")
    .sort((a, b) => b.rss_kib - a.rss_kib);
}

function mostMemory(summary: ActivitySummary): Question {
  const [first, second] = largestItems(summary);
  const answer =
    first === undefined
      ? ["Nothing stands out right now."]
      : [
          `Right now, "${first.name}" uses the most (${formatKib(first.rss_kib)})` +
            (second === undefined ? "." : `, then "${second.name}" (${formatKib(second.rss_kib)}).`),
          first.kind === "CHAT"
            ? "A chat uses memory the whole time it's running, even while it waits for you, so stopping chats you're done with frees the most."
            : "A chat uses memory the whole time it's running, even while it waits for you.",
        ];
  return { id: "most-memory", question: "What's using the most memory?", answer };
}

/** The questions for what the page is showing: the most pressing first, three at most. */
export function questionsFor(summary: ActivitySummary): Question[] {
  const status = summary.memory?.status ?? "COMFORTABLE";
  const hasFirstToClose = summary.likely_first_to_close !== null;
  if (status === "COMFORTABLE") return [mostMemory(summary), MORE_MEMORY, ...(hasFirstToClose ? [WHY_FIRST] : [])];
  return [FILLS_UP, FREE_UP, MORE_MEMORY];
}

function firstToCloseName(summary: ActivitySummary): string | null {
  const pick = summary.likely_first_to_close;
  if (pick === null) return null;
  const all = [...summary.chats, ...summary.apps, ...summary.services];
  return all.find((item) => item.item_id === pick.item_id)?.name ?? null;
}

/**
 * The message drafted into the user's chat: their question first, then what the page shows. The chat puts a draft
 * above whatever its composer already holds, so the question has to lead for two drafts to look different.
 */
export function chatDraftFor(summary: ActivitySummary, question: string | null): string {
  const context: string[] = [];
  if (summary.memory !== null) {
    const headline = headlineFor(summary.memory);
    context.push(
      `it says "${headline.title}" (${formatBytes(summary.memory.used_bytes)} of ${formatBytes(summary.memory.limit_bytes)} in use)`,
    );
  }
  const firstName = firstToCloseName(summary);
  if (firstName !== null) context.push(`it marks "${firstName}" as likely to be closed first`);
  const page =
    context.length === 0
      ? "I'm looking at System Monitor."
      : `I'm looking at System Monitor: ${context.join(", and ")}.`;
  return question === null
    ? `${page} Can you help me understand what I'm seeing?`
    : `${question} ${page} Please explain using what's actually running.`;
}

/** What a screen reader says for a row, and what a right-click "Explain..." reference carries as its label. */
export function rowLabel(item: ActivityItem, stateLine: string, isFirstToClose: boolean, share: string): string {
  const size = item.rss_kib > 0 ? `${formatKib(item.rss_kib)}, ${share} of memory in use` : "not running";
  return [item.name, size, stateLine, ...(isFirstToClose ? ["likely closed first if memory runs out"] : [])].join(
    ", ",
  );
}
