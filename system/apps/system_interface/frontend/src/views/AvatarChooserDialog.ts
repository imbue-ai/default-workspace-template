/**
 * The avatar chooser (pinned-taskbar-entries plan section 4.7): a dialog on the shared Modal with every
 * design's still image, the workspace's choice marked, a link to the chosen design's original, and
 * "Design your own...", which drafts the design prompt into the pinned window whose app declares a launch
 * path at its home path taking a draft (the shell names no app), unsent. Choosing a design writes the
 * workspace's selection; every window, this one included, follows the shell's broadcast.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { Modal } from "@imbue/workspace-ui/src/components/Modal";
import { avatarImageUrl, avatarSourceUrl } from "../model/api";
import type { AvatarDesign } from "../model/records";

/** The first message of the chat "Design your own..." starts. */
export const NO_DRAFT_TARGET_REASON = "No pinned window on this desktop takes a draft";

export const AVATAR_DESIGN_PROMPT =
  "I'd like to design my own desktop avatar. Help me draw it, show me a preview, " +
  "and replace my current avatar only after I approve the design. " +
  "Here's how I'd like it to look: ";

const CHOOSER_WIDTH_PX = 460;

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
}

function designCell(design: AvatarDesign, attrs: AvatarChooserDialogAttrs): m.Vnode {
  const isSelected = design.id === attrs.selected;
  return m(
    "button",
    {
      key: design.id,
      type: "button",
      "data-avatar-design": design.id,
      "aria-pressed": isSelected ? "true" : "false",
      class:
        "flex min-w-0 cursor-pointer flex-col items-center gap-1 rounded-md border p-2 text-center type-helper " +
        "hover:bg-fill-hover focus-visible:outline-accent " +
        (isSelected ? "border-accent bg-fill-active text-primary" : "border-transparent text-secondary"),
      onclick: () => attrs.onSelect(design.id),
    },
    [
      m("img", { src: avatarImageUrl(design.id, "idle", true), alt: "", draggable: false, class: "size-14" }),
      m("span", { class: "w-full truncate" }, design.label),
    ],
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
                // Present and empty: the route's Content-Disposition names the file.
                download: "",
              },
              "Original SVG",
            ),
        designOwnRow(attrs),
      ],
    );
  },
};
