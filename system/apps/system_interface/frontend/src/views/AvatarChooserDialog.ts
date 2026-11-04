/**
 * The avatar chooser (pinned-taskbar-entries plan section 4.7): a dialog on the shared Modal with every
 * design's still image (the workspace's own character drawn live, in the theme's avatar colour), the workspace's
 * choice marked, a link to the chosen design's original, and
 * "Design your own...", which drafts the design prompt into the pinned window whose app declares a launch
 * path at its home path taking a draft (the shell names no app), unsent. Choosing a design writes the
 * workspace's selection; every window, this one included, follows the shell's broadcast.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { Modal } from "@imbue/workspace-ui/src/components/Modal";
import { startDownload } from "@imbue/workspace-ui/src/download";
import { TILE_PART, partAttrs } from "@imbue/workspace-ui/src/themes/parts";
import { avatarImageUrl, avatarSourceUrl } from "../model/api";
import type { AvatarDesign } from "../model/records";
import { IMBUE_CHARACTER_DESIGN_ID, ImbueCharacter } from "./character/ImbueCharacter";

/** The disabled "Design your own..." button's tooltip. */
export const NO_DRAFT_TARGET_REASON = "No pinned window on this desktop takes a draft";

/** The prompt "Design your own..." drafts, unsent, into the pinned window's chat. */
export const AVATAR_DESIGN_PROMPT =
  "I'd like to design my own desktop avatar. Help me draw it, show me a preview, " +
  "and replace my current avatar only after I approve the design. " +
  "Here's how I'd like it to look: ";

const CHOOSER_WIDTH_PX = 460;
/** The character's width in its 56 px tile: the still every other design is previewed as fills 0.8 of its box. */
const CHARACTER_PREVIEW_SIZE_PX = 45;

export interface AvatarChooserDialogAttrs {
  /** The designs on offer; null while they load. */
  readonly designs: readonly AvatarDesign[] | null;
  readonly loadError: string | null;
  /** The workspace's current design. */
  readonly selected: string;
  readonly onSelect: (design: string) => void;
  /** Null when no app declares a launch path that takes a message. */
  readonly onDesignOwn: (() => void) | null;
  readonly onClose: () => void;
  /** Tell the user the chosen design's original did not download. */
  readonly onDownloadFailed: (note: string) => void;
}

/** A design's preview: its still image, or, for the workspace's own character, the character itself, which wears
 *  the theme's avatar colour as it does in the taskbar (an image of it could not). */
function designPicture(design: AvatarDesign): m.Children {
  if (design.id === IMBUE_CHARACTER_DESIGN_ID) {
    return m(
      "span",
      { class: "flex size-14 items-center justify-center" },
      m(ImbueCharacter, { mood: "idle", interactive: false, shadow: false, size: CHARACTER_PREVIEW_SIZE_PX }),
    );
  }
  return m("img", {
    ...partAttrs("avatar"),
    "data-avatar-kind": "image",
    src: avatarImageUrl(design.id, "idle", true),
    alt: "",
    draggable: false,
    class: "size-14",
  });
}

function designCell(design: AvatarDesign, attrs: AvatarChooserDialogAttrs): m.Vnode {
  const isSelected = design.id === attrs.selected;
  return m(
    "button",
    {
      key: design.id,
      type: "button",
      "data-avatar-design": design.id,
      ...TILE_PART,
      "aria-pressed": isSelected ? "true" : "false",
      class:
        "flex min-w-0 cursor-pointer flex-col items-center gap-1 rounded-md border p-2 text-center type-helper " +
        "hover:bg-fill-hover focus-visible:outline-accent " +
        (isSelected ? "border-accent bg-fill-active text-primary" : "border-transparent text-secondary"),
      onclick: () => attrs.onSelect(design.id),
    },
    [designPicture(design), m("span", { class: "w-full truncate" }, design.label)],
  );
}

function designGrid(attrs: AvatarChooserDialogAttrs): m.Children {
  if (attrs.loadError !== null) {
    return m("p", { class: "type-helper text-danger", role: "alert" }, attrs.loadError);
  }
  if (attrs.designs === null) return m("p", { class: "type-helper text-faint", role: "status" }, "Loading designs…");
  return m(
    "div",
    { class: "grid grid-cols-4 gap-1" },
    attrs.designs.map((design) => designCell(design, attrs)),
  );
}

function designOwnRow(attrs: AvatarChooserDialogAttrs): m.Children {
  const isDisabled = attrs.onDesignOwn === null;
  return m("div", { class: "mt-4 border-t border-default pt-3" }, [
    m(
      "span",
      { class: "inline-block", ...hoverTooltipAttrs(isDisabled ? NO_DRAFT_TARGET_REASON : null) },
      m(
        Button,
        {
          extra: "avatar-design-own",
          disabled: isDisabled,
          onclick: () => attrs.onDesignOwn?.(),
        },
        "Design your own...",
      ),
    ),
    m(
      "p",
      { class: "type-helper mt-1 text-secondary" },
      "Puts a request to draw your avatar into your chat, for you to edit and send.",
    ),
  ]);
}

export const AvatarChooserDialog: m.Component<AvatarChooserDialogAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    const selected = attrs.designs?.find((design) => design.id === attrs.selected);
    return m(
      Modal,
      {
        width: CHOOSER_WIDTH_PX,
        title: "Choose avatar",
        onDismiss: attrs.onClose,
        onEscape: attrs.onClose,
        card: { "data-avatar-chooser": "", role: "dialog", "aria-label": "Choose avatar" },
        actions: m(
          Button,
          {
            variant: "primary",
            extra: "avatar-chooser-done",
            onclick: attrs.onClose,
            // The menu row that opened the dialog is gone on the same redraw, so focus starts in the dialog.
            oncreate: (created) => (created.dom as HTMLButtonElement).focus(),
          },
          "Done",
        ),
      },
      [
        designGrid(attrs),
        // Only a design the catalog lists has an original to fetch.
        selected === undefined
          ? null
          : m(
              "a",
              {
                class: "type-helper mt-2 inline-block text-secondary underline",
                "data-avatar-source": "",
                href: avatarSourceUrl(selected.id),
                ...hoverTooltipAttrs(selected.source_path),
                onclick: (event: MouseEvent) => {
                  event.preventDefault();
                  startDownload(document, avatarSourceUrl(selected.id), `${selected.id}.svg`, attrs.onDownloadFailed);
                },
              },
              "Original SVG",
            ),
        designOwnRow(attrs),
      ],
    );
  },
};
