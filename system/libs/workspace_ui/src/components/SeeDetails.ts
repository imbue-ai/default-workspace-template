import m from "mithril";
import { TEXT_HELPER_SIZE } from "./typography";

/**
 * The collapsed "See details" under a failure's sentence: everything underneath it (a status, the server's own text,
 * command output), kept for a technical reader and a bug report rather than put on the visible line. Its colours are
 * inherited, so it reads the same in a light dialog and on a dark toast. Renders nothing when there is nothing to show.
 */
export interface SeeDetailsAttrs {
  readonly details: string | null;
}

export const SeeDetails: m.Component<SeeDetailsAttrs> = {
  view(vnode) {
    const { details } = vnode.attrs;
    if (details === null || details.trim() === "") return null;
    return m("details", { class: `see-details ${TEXT_HELPER_SIZE} max-w-full` }, [
      m(
        "summary",
        {
          class:
            "see-details-toggle w-fit cursor-pointer underline-offset-2 opacity-80 select-none hover:underline " +
            "hover:opacity-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
        },
        "See details",
      ),
      m(
        "pre",
        {
          class:
            "see-details-text mt-1.5 max-h-[30vh] overflow-auto rounded-sm bg-black/10 p-2 font-mono " +
            "whitespace-pre-wrap opacity-90 wrap-anywhere select-text",
        },
        details,
      ),
    ]);
  },
};
