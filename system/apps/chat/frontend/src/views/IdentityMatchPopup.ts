/**
 * The "Is this you?" popup: who the onboarding agent's background web look-up found for the user, each
 * with the page it came from, so the user can say which one is them, or that none is.
 *
 * It opens over the chat as soon as the look-up's candidates arrive (``models/IdentityMatches``), whether or
 * not the agent is mid-reply. "This is me" and "None of these" are the two answers; either reaches the agent
 * as a message in the user's words. Escape or a press outside sets it aside on this page without answering,
 * and a reload shows it again until it is answered.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { MODAL_MESSAGE_CLASS, MODAL_TITLE_CLASS, Modal } from "@imbue/workspace-ui/src/components/Modal";
import { describeRequestError } from "@imbue/workspace-ui/src/models/request-error";
import type { IdentityCandidate, IdentityMatch } from "../models/IdentityMatches";
import {
  confirmIdentityMatch,
  getPendingIdentityMatch,
  rejectIdentityMatch,
  setIdentityMatchAside,
  sourceSite,
} from "../models/IdentityMatches";

const TITLE_ID = "identity-match-title";

const BADGE =
  "flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-accent-light text-accent " +
  "animate-[modal-card-in_var(--dur-slow)_cubic-bezier(0.16,1,0.3,1)]";
const ROW_STACK = "mb-2 flex flex-col gap-2";
const ROW =
  "identity-candidate flex items-center gap-3 rounded-lg border border-default bg-surface p-3 shadow-raised " +
  "transition-[border-color] duration-(--dur-base) hover:border-accent " +
  "animate-[modal-card-in_var(--dur-slow)_cubic-bezier(0.16,1,0.3,1)]";
const ROW_TEXT = "min-w-0 flex-1";
const ROW_NAME = "block type-body font-semibold text-primary";
const ROW_SUMMARY = "mt-0.5 block type-helper text-primary";
const ROW_SOURCE =
  "mt-1 inline-flex max-w-full items-center gap-1 type-helper text-secondary underline-offset-2 hover:text-accent hover:underline";
const FOOTNOTE = "mb-4 type-helper text-secondary";
const ERROR = "mb-3 type-helper text-danger";
// Each row slides in a beat after the one above it.
const ROW_STAGGER_MS = 70;

type Busy = { kind: "confirm"; index: number } | { kind: "reject" } | null;

export function IdentityMatchPopup(): m.Component<{ chatId: string }> {
  let busy: Busy = null;
  let error: string | null = null;
  let shownMatchId: string | null = null;

  async function run(action: () => Promise<void>, next: Busy): Promise<void> {
    busy = next;
    error = null;
    try {
      await action();
    } catch (failure) {
      error = `That didn't go through: ${describeRequestError(failure)}`;
    } finally {
      busy = null;
      m.redraw();
    }
  }

  function candidateRow(match: IdentityMatch, candidate: IdentityCandidate, index: number): m.Children {
    const isThisBusy = busy?.kind === "confirm" && busy.index === index;
    return m(
      "div",
      { class: ROW, key: index, style: `animation-delay: ${index * ROW_STAGGER_MS}ms; animation-fill-mode: both` },
      [
        m("div", { class: ROW_TEXT }, [
          m("span", { class: ROW_NAME }, candidate.name),
          m("span", { class: ROW_SUMMARY }, candidate.summary),
          m(
            "a",
            {
              class: ROW_SOURCE,
              href: candidate.source_url,
              target: "_blank",
              rel: "noopener noreferrer",
              title: candidate.source_title,
            },
            [
              m("span", { class: "truncate" }, sourceSite(candidate.source_url)),
              m.trust(icon("external-link", { size: 12 })),
            ],
          ),
        ]),
        m(
          Button,
          {
            variant: "primary",
            sm: true,
            disabled: busy !== null,
            onclick: () => void run(() => confirmIdentityMatch(match.match_id, index), { kind: "confirm", index }),
          },
          isThisBusy ? "Sending…" : "This is me",
        ),
      ],
    );
  }

  return {
    view(vnode) {
      const match = getPendingIdentityMatch(vnode.attrs.chatId);
      if (match === null) return null;
      if (match.match_id !== shownMatchId) {
        shownMatchId = match.match_id;
        busy = null;
        error = null;
      }
      const setAside = (): void => {
        if (busy === null) setIdentityMatchAside(match.match_id);
      };
      return m(
        Modal,
        {
          width: 480,
          onDismiss: setAside,
          onEscape: setAside,
          card: {
            role: "dialog",
            "aria-modal": "true",
            "aria-labelledby": TITLE_ID,
            "data-e2e": "identity-match-popup",
          },
          header: [
            m("span", { class: BADGE }, m.trust(icon("search", { size: 18 }))),
            m(
              "h3",
              { class: MODAL_TITLE_CLASS, id: TITLE_ID },
              match.candidates.length === 1 ? "Is this you?" : "Is one of these you?",
            ),
          ],
          actions: [
            m(
              Button,
              {
                variant: "secondary",
                disabled: busy !== null,
                onclick: () => void run(() => rejectIdentityMatch(match.match_id), { kind: "reject" }),
              },
              busy?.kind === "reject" ? "Sending…" : "None of these",
            ),
          ],
        },
        [
          m("p", { class: MODAL_MESSAGE_CLASS }, `I looked you up on the web as "${match.searched_for}".`),
          m(
            "div",
            { class: ROW_STACK },
            match.candidates.map((candidate, index) => candidateRow(match, candidate, index)),
          ),
          m("p", { class: FOOTNOTE }, "Nothing from the search is used unless you pick it."),
          error === null ? null : m("p", { class: ERROR, role: "alert" }, error),
        ],
      );
    },
  };
}
