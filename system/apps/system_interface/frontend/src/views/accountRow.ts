/**
 * One account row in a provider flyout, shared by the new-tab picker and the chat's model card.
 *
 * The two menus differ in what a row CLICK does -- the picker selects, the card's rows are
 * locked -- but their trailing controls are the same three: a rename pencil, a sign-out bin,
 * and the tick marking the current account. Those controls carry the only fiddly logic here
 * (arming, an inline field, three ways out of an edit), so they live in one place rather than
 * being typed out twice and drifting.
 *
 * State stays with the CALLER. Each menu already owns the lifecycle its controls hang off --
 * an armed "Remove?" belongs to the flyout it was armed in and has to be cleared when that
 * flyout closes -- and a module-level store here could not see either menu closing.
 */

import m from "mithril";
import { deleteAccount, loadAccounts, renameAccount } from "../models/Providers";
import type { ProviderAccount } from "../models/Providers";
import { icon } from "./icons";
import * as css from "./modelCardStyles";

/** Which row, if any, is showing its controls in a non-resting state. Both are per-flyout. */
export interface AccountRowState {
  /** The row whose bin is armed into "Remove?", or null. */
  confirmingRemoval: string | null;
  /** The row showing its inline rename field, or null. */
  renamingId: string | null;
  /** What is in that field. Held beside `renamingId` rather than read off the DOM, because
   *  a redraw between keystrokes would otherwise reset the field to the stored name. */
  renameDraft: string;
}

export function emptyAccountRowState(): AccountRowState {
  return { confirmingRemoval: null, renamingId: null, renameDraft: "" };
}

export interface AccountRowOptions {
  row: ProviderAccount;
  /** The account this menu marks with a tick. */
  isCurrent: boolean;
  /** Classes for the row button -- the two menus style a non-current row differently. */
  rowClass: string;
  /** Anything else the row button needs: a tooltip, `aria-disabled`, and so on. */
  rowAttrs?: m.Attributes;
  onSelect: () => void;
  state: AccountRowState;
  /** Run after a rename or a removal lands, for a caller that has to close or refresh. */
  onChanged?: () => void;
}

/** Put a row into its rename field, seeded with the name it is showing now.
 *
 * Seeded from `provider` rather than `name` so the field starts from what the user can SEE.
 * A never-renamed account has an empty `name`, and opening an empty field over a row that
 * reads "Anthropic" looks like the name was just lost. */
function beginRename(state: AccountRowState, row: ProviderAccount): void {
  state.renamingId = row.id;
  // Seeded from the user's own name, or left EMPTY -- never from `provider`, which carries the
  // disambiguating number ("Anthropic 2"). Seeding that meant pressing the pencil on an unnamed
  // duplicate and hitting Enter filed "Anthropic 2" as a real chosen name: it then survives the
  // account it was counting against being deleted, and the numbering starts counting around a
  // literal. The placeholder shows what the row is called while the field is blank.
  state.renameDraft = row.name;
}

/** Take the field down. Redraws itself: the paths out that file nothing -- Escape, and a
 *  commit of a name that says nothing new -- have no response to redraw on afterwards, and
 *  these menus are portalled, so without this the field simply stays on screen. */
function endRename(state: AccountRowState): void {
  state.renamingId = null;
  state.renameDraft = "";
  m.redraw();
}

/** File what was typed, unless it says nothing new.
 *
 * An emptied field is a real instruction, not a slip: clearing the name is the only way back
 * to the provider's own, so it is sent rather than dropped. Compared against `name`, the
 * STORED value -- comparing against the displayed `provider` would read "clear it" as "no
 * change" for every account that never had a name, and, worse, would read retyping a chosen
 * name unchanged as a request to clear it, since `provider` is that chosen name already. */
function commitRename(state: AccountRowState, row: ProviderAccount, onChanged?: () => void): void {
  const typed = state.renameDraft.trim();
  endRename(state);
  if (typed === row.name) return;
  // `.catch` is not optional here: a 404 is ordinary -- a double-click, or the same account
  // open in two menus and removed from one -- and without it the rejection is unhandled and
  // the row silently keeps a name the server does not have. Reloading is the correction: it
  // shows whatever is actually there.
  void renameAccount(row.id, typed)
    .then(() => {
      onChanged?.();
    })
    .catch((error: unknown) => {
      console.warn(`Could not rename account ${row.id}`, error);
      void loadAccounts();
    })
    .finally(() => {
      m.redraw();
    });
}

/** The inline field a row becomes while it is being renamed.
 *
 * Commits on blur, which is what makes every way out of the edit -- Enter, clicking another
 * row, the flyout closing under it -- end the same way without a handler each. Escape
 * discards instead, and the `renamingId` guard stops the blur that its own removal fires
 * from immediately re-committing what was just discarded. */
function renameField(opts: AccountRowOptions): m.Vnode {
  const { row, state } = opts;
  return m("input", {
    type: "text",
    class: css.ROW_RENAME_INPUT,
    value: state.renameDraft,
    placeholder: row.provider,
    "aria-label": `Rename ${row.provider}`,
    oncreate: (vnode: m.VnodeDOM) => {
      const input = vnode.dom as HTMLInputElement;
      input.focus();
      input.select();
    },
    onclick: (event: MouseEvent) => event.stopPropagation(),
    oninput: (event: InputEvent) => {
      state.renameDraft = (event.target as HTMLInputElement).value;
    },
    onblur: () => {
      if (state.renamingId !== row.id) return;
      commitRename(state, row, opts.onChanged);
    },
    onkeydown: (event: KeyboardEvent) => {
      if (event.key === "Enter") (event.target as HTMLInputElement).blur();
      else if (event.key === "Escape") endRename(state);
    },
  });
}

/** One account row, complete with its trailing controls. Keyed -- both callers render these
 *  as a list, and mithril refuses a fragment mixing keyed vnodes with anything else. */
export function accountRow(opts: AccountRowOptions): m.Vnode {
  const { row, state, isCurrent } = opts;
  const arming = state.confirmingRemoval === row.id;
  const renaming = state.renamingId === row.id;

  // Mid-rename the row is only the field: the tick, the bin and the pencil all stand down so
  // the name has the full width to be typed in, and so nothing destructive sits under a
  // pointer that is there to click into text.
  if (renaming) {
    return m("div", { key: row.id, class: css.ROW_WRAP }, renameField(opts));
  }

  return m("div", { key: row.id, class: css.ROW_WRAP }, [
    m(
      "button",
      {
        type: "button",
        class: opts.rowClass,
        ...(opts.rowAttrs ?? {}),
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          opts.onSelect();
        },
      },
      [
        m("span", { class: css.FLYOUT_ROW_NAME }, row.provider),
        m("span", { class: css.FLYOUT_ROW_SUB }, `(${row.harness_label})`),
      ],
    ),
    // Siblings of the row button rather than children -- buttons cannot nest -- each pinned
    // to its own offset from the right edge so none of the three ever displaces another.
    isCurrent
      ? m("span", { class: css.FLYOUT_CHECK_PINNED }, m.trust(icon("check", { size: 13, strokeWidth: 2.5 })))
      : null,
    m(
      "button",
      {
        type: "button",
        class: arming ? css.ROW_TRASH_ARMED : css.ROW_TRASH,
        "aria-label": arming ? `Confirm removing ${row.provider}` : `Sign out of ${row.provider}`,
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          if (!arming) {
            state.confirmingRemoval = row.id;
            return;
          }
          state.confirmingRemoval = null;
          void deleteAccount(row.id)
            .then(() => opts.onChanged?.())
            .catch((error: unknown) => {
              // Already gone is the common case, and the list is the honest answer either way.
              console.warn(`Could not remove account ${row.id}`, error);
              void loadAccounts();
            })
            .finally(() => {
              m.redraw();
            });
        },
      },
      arming ? "Remove?" : m.trust(icon("trash", { size: 13 })),
    ),
    // Withheld while the bin is armed: "Remove?" is wide enough to reach under the pencil,
    // and a row asking whether to delete itself should not also be offering to rename.
    arming
      ? null
      : m(
          "button",
          {
            type: "button",
            class: css.ROW_PENCIL,
            "aria-label": `Rename ${row.provider}`,
            onclick: (event: MouseEvent) => {
              event.stopPropagation();
              state.confirmingRemoval = null;
              beginRename(state, row);
            },
          },
          m.trust(icon("edit", { size: 13 })),
        ),
  ]);
}
