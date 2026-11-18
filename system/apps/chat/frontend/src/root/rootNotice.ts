/**
 * The chat root's notice: a failure of something done from the chat list, told in a dialog over the list rather
 * than a browser alert. A fixed line saying what did not happen, the chat app's reason under it, and a button to
 * try again where trying again can help. Notices raised while one is up wait their turn behind it.
 */

import m from "mithril";
import { makeNoticeDialog } from "@imbue/workspace-ui/src/components/NoticeDialog";
import { HttpError } from "@imbue/workspace-ui/src/models/http";

export interface RootNoticeRetry {
  readonly label: string;
  readonly run: () => void;
}

export interface RootNotice {
  readonly title: string;
  readonly body: readonly (string | null)[];
  readonly retry: RootNoticeRetry | null;
}

interface QueuedNotice {
  readonly id: number;
  readonly notice: RootNotice;
}

let queue: readonly QueuedNotice[] = [];
let nextId = 1;

export function raiseRootNotice(notice: RootNotice): void {
  queue = [...queue, { id: nextId, notice }];
  nextId += 1;
  m.redraw();
}

/** Whether a notice is up, so what lies under it leaves Escape to it. */
export function isRootNoticeUp(): boolean {
  return queue.length > 0;
}

function closeRootNotice(): void {
  queue = queue.slice(1);
}

/** A notice's body for a list action that failed: ``refused``, a fixed line saying what did not happen, with the chat
 *  app's reason under it; or ``unanswered`` when no answer came, so whether it happened is not known. */
export function failureBody(
  error: unknown,
  lines: { readonly refused: string; readonly unanswered: string },
): readonly string[] {
  return error instanceof HttpError ? [lines.refused, error.message] : [lines.unanswered];
}

/** Mounted once by the chat root, over everything else. */
export function RootNoticeView(): m.Component {
  const dialog = makeNoticeDialog();
  return {
    view() {
      const queued = queue[0];
      if (queued === undefined) return null;
      const { notice } = queued;
      const retry = notice.retry;
      // In a keyed list, so the next notice in the queue mounts afresh and takes the focus.
      return [
        m(dialog, {
          key: queued.id,
          title: notice.title,
          body: notice.body,
          dismissLabel: "OK",
          onDismiss: closeRootNotice,
          actions:
            retry === null
              ? []
              : [
                  {
                    label: retry.label,
                    run: () => {
                      closeRootNotice();
                      retry.run();
                    },
                  },
                ],
        }),
      ];
    },
  };
}
