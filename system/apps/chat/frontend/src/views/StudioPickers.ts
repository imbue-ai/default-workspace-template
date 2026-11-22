/**
 * The pickers of a new workspace's first conversation (specs/chat-suggestions in mngr): what the chat draws where
 * an agent's message carries ``<studio_category_picker/>`` or ``<studio_connector_picker/>`` on a line of its own
 * (``splitStudioWidgets``).
 *
 * A picker is a set of pills, and a click on one edits the composer's draft: it adds the pill's label and a comma,
 * or takes them out again. The pill reads as pressed while the draft holds them, so the picker has no state of its
 * own and comes back as it was after a redraw or a reload. The user sends the draft themselves, with anything of
 * their own typed in, which is what each picker's last pill, "Other", says: it works as every pill does, but puts a
 * prompt to write over in the draft in place of its own label.
 *
 * A picker takes clicks only in the reply the chat waits on its user to answer; in an earlier message it is drawn
 * disabled. It reads the same draft there, so what was picked from it no longer shows once that was sent.
 */

import m from "mithril";
import type { StudioWidgetKind } from "../studio-widgets";
import { isComposerFragmentInDraft, toggleComposerFragment } from "../models/ComposerDraft";
import { showPickerSendHintOnce } from "./PickerSendHint";
import { serviceMarkUrl } from "./service-marks";

interface CategoryColumn {
  title: string;
  categories: readonly string[];
}

/** What a workspace may be for, in the three columns the category picker draws. */
export const PICKER_CATEGORY_COLUMNS: readonly CategoryColumn[] = [
  {
    title: "Work",
    categories: [
      "Knowledge work",
      "Team projects",
      "Sales",
      "Marketing",
      "Operations",
      "Recruiting",
      "Fundraising",
      "Small business",
    ],
  },
  {
    title: "Life",
    categories: [
      "Personal",
      "Money",
      "Health",
      "Home & family",
      "Travel & events",
      "Job search",
      "School",
      "Notes & archives",
    ],
  },
  {
    title: "Creative",
    categories: ["Writing", "Art & design", "Film & video", "Game dev", "Books", "Movies", "Music"],
  },
];

/** The label of the pill that ends each picker. It names no category and no app, so it is kept out of their lists. */
export const PICKER_OTHER_LABEL = "Other";
/** What "Other" puts in the draft from the category picker, and from the connector picker. */
export const CATEGORY_PICKER_OTHER_PROMPT = "Write whatever you want!";
export const CONNECTOR_PICKER_OTHER_PROMPT = "Write whatever data you want here!";

interface Connector {
  name: string;
  /** The service whose bundled mark is drawn beside the name (``service-marks.ts``); a service with no bundled
   *  mark is drawn by its name alone. */
  service: string;
}

/** The apps the connector picker offers, in the order it draws them. */
export const PICKER_CONNECTORS: readonly Connector[] = [
  { name: "Gmail", service: "google-gmail" },
  { name: "Google Calendar", service: "google-calendar" },
  { name: "GitHub", service: "github" },
  { name: "Slack", service: "slack" },
  { name: "Notion", service: "notion-mcp" },
  { name: "Google Drive", service: "google-drive" },
  { name: "Google Sheets", service: "google-sheets" },
  { name: "Stripe", service: "stripe" },
  { name: "Instagram", service: "instagram" },
  { name: "YouTube", service: "youtube" },
  { name: "Strava", service: "strava" },
  { name: "HubSpot", service: "hubspot" },
  { name: "Asana", service: "asana" },
  { name: "Todoist", service: "todoist" },
  { name: "Linear", service: "linear" },
];

export interface StudioPickerAttrs {
  kind: StudioWidgetKind;
  chatId: string;
  /** Whether the picker takes clicks: only in the reply the chat waits on its user to answer. */
  isLive: boolean;
}

interface PillAttrs {
  chatId: string;
  label: string;
  /** What a click puts in the draft: the label, for every pill but "Other". */
  fragment: string;
  isLive: boolean;
  /** Drawn ahead of the label. */
  mark: m.Children;
  /** Run when a click added the fragment to the draft. */
  onAdded: (() => void) | null;
}

function pill(attrs: PillAttrs): m.Vnode {
  const isPressed = isComposerFragmentInDraft(attrs.chatId, attrs.fragment);
  return m(
    "button",
    {
      key: attrs.label,
      type: "button",
      class: isPressed ? "studio-picker-pill studio-picker-pill--pressed" : "studio-picker-pill",
      "aria-pressed": isPressed ? "true" : "false",
      disabled: !attrs.isLive,
      onclick: () => {
        toggleComposerFragment(attrs.chatId, attrs.fragment);
        if (!isPressed) attrs.onAdded?.();
      },
    },
    [attrs.mark, m("span", { class: "min-w-0" }, attrs.label)],
  );
}

function categoryPicker(chatId: string, isLive: boolean): m.Vnode {
  return m(
    "div",
    {
      class: "studio-picker studio-category-picker grid grid-cols-3 gap-x-4 gap-y-3 max-[520px]:grid-cols-1",
      "data-studio-picker": "category",
      role: "group",
      "aria-label": "What do you want to use this Studio for?",
    },
    PICKER_CATEGORY_COLUMNS.map((column, index) =>
      m("div", { key: column.title, class: "studio-picker-column flex min-w-0 flex-col items-start gap-1.5" }, [
        m("div", { key: "title", class: "studio-picker-heading" }, column.title),
        ...column.categories.map((category) =>
          pill({ chatId, label: category, fragment: category, isLive, mark: null, onAdded: showPickerSendHintOnce }),
        ),
        ...(index === PICKER_CATEGORY_COLUMNS.length - 1
          ? [
              pill({
                chatId,
                label: PICKER_OTHER_LABEL,
                fragment: CATEGORY_PICKER_OTHER_PROMPT,
                isLive,
                mark: null,
                onAdded: showPickerSendHintOnce,
              }),
            ]
          : []),
      ]),
    ),
  );
}

function connectorMark(connector: Connector): m.Children {
  const url = serviceMarkUrl(connector.service);
  return url === null ? null : m("img", { class: "studio-picker-mark", src: url, alt: "", draggable: false });
}

function connectorPicker(chatId: string, isLive: boolean): m.Vnode {
  return m(
    "div",
    {
      class: "studio-picker studio-connector-picker flex flex-wrap items-center gap-1.5",
      "data-studio-picker": "connector",
      role: "group",
      "aria-label": "Which apps do you use most?",
    },
    [
      ...PICKER_CONNECTORS.map((connector) =>
        pill({
          chatId,
          label: connector.name,
          fragment: connector.name,
          isLive,
          mark: connectorMark(connector),
          onAdded: null,
        }),
      ),
      pill({
        chatId,
        label: PICKER_OTHER_LABEL,
        fragment: CONNECTOR_PICKER_OTHER_PROMPT,
        isLive,
        mark: null,
        onAdded: null,
      }),
    ],
  );
}

export const StudioPicker: m.Component<StudioPickerAttrs> = {
  view({ attrs }) {
    switch (attrs.kind) {
      case "category_picker":
        return categoryPicker(attrs.chatId, attrs.isLive);
      case "connector_picker":
        return connectorPicker(attrs.chatId, attrs.isLive);
    }
  },
};
