/**
 * The Components group: every shared control of the library in its variants and states, rendered by the library
 * itself, so what shows here is what every app draws. The interactive ones (the menu, the dropdown, the segmented
 * control, the dialogs, the toasts) work: their state lives in ``ComponentDemos``.
 */

import m from "mithril";

import { DestroyConfirmDialog } from "@imbue/workspace-ui/src/DestroyConfirmDialog";
import { activityDotClass } from "@imbue/workspace-ui/src/components/activityDot";
import { badgeClass, type BadgeTone } from "@imbue/workspace-ui/src/components/Badge";
import { bannerClass } from "@imbue/workspace-ui/src/components/banner";
import { Button, type ButtonVariant } from "@imbue/workspace-ui/src/components/Button";
import { Dropdown } from "@imbue/workspace-ui/src/components/dropdown";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon, spinnerIcon } from "@imbue/workspace-ui/src/components/icons";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { createMenu, type Menu, type MenuRow } from "@imbue/workspace-ui/src/components/menu";
import { MODAL_LABEL_CLASS, MODAL_MESSAGE_CLASS, Modal } from "@imbue/workspace-ui/src/components/Modal";
import { makeNoticeDialog } from "@imbue/workspace-ui/src/components/NoticeDialog";
import { SegmentedControl } from "@imbue/workspace-ui/src/components/segmentedControl";
import { Toasts } from "@imbue/workspace-ui/src/components/toasts";
import { ToastQueue } from "@imbue/workspace-ui/src/models/toasts";
import { BADGE_PART, FIELD_PART } from "@imbue/workspace-ui/src/themes/parts";

import { caption, code, demoRow, type CatalogGroup, type CatalogSection } from "./catalog";

export const BUTTON_VARIANTS: readonly ButtonVariant[] = [
  "primary",
  "secondary",
  "ghost",
  "destructive",
  "ghost-destructive",
  "inverse",
  "stop",
];

export const BADGE_TONES: readonly BadgeTone[] = ["neutral", "accent", "danger", "warning", "success"];

type Density = "comfortable" | "compact" | "dense";
type ViewMode = "grid" | "list" | "columns";
type Provider = "claude" | "openrouter" | "local";
type OpenDialog = "modal" | "notice" | "destroy" | null;

const PROVIDERS = [
  { value: "claude" as const, label: "Claude", detail: "anthropic" },
  { value: "openrouter" as const, label: "OpenRouter", detail: "openrouter/qwen" },
  { value: "local" as const, label: "This computer" },
];

/** The demos' state: what each interactive control has picked, and which dialog is up. */
export class ComponentDemos {
  isToggleOn = true;
  density: Density = "comfortable";
  viewMode: ViewMode = "grid";
  provider: Provider | null = null;
  model = "claude-sonnet";
  showHidden = false;
  showPreview = true;
  lastMenuPick = "nothing yet";
  openDialog: OpenDialog = null;
  readonly toasts = new ToastQueue(() => m.redraw());
  readonly menu: Menu = createMenu({ placement: "below", minWidth: 220, extraClass: "catalog-menu" });
  readonly noticeDialog = makeNoticeDialog();

  dispose(): void {
    this.menu.dispose();
  }

  menuRows(): readonly MenuRow[] {
    const pick = (label: string) => (): void => {
      this.lastMenuPick = label;
    };
    const models = ["claude-opus", "claude-sonnet", "claude-haiku"];
    return [
      { kind: "action", key: "rename", label: "Rename", icon: "edit", onSelect: pick("Rename") },
      { kind: "action", key: "duplicate", label: "Duplicate", icon: "copy", onSelect: pick("Duplicate") },
      {
        kind: "submenu",
        key: "model",
        label: "Model",
        value: this.model,
        rows: () =>
          models.map((name) => ({
            kind: "action" as const,
            key: name,
            label: name,
            trailing: name === this.model ? m.trust(icon("check", { size: 14 })) : null,
            onSelect: () => {
              this.model = name;
              this.lastMenuPick = `Model: ${name}`;
            },
          })),
      },
      { kind: "value", key: "created", label: "Created", value: "2 days ago" },
      { kind: "divider" },
      {
        kind: "check",
        key: "hidden",
        label: "Show hidden files",
        isChecked: this.showHidden,
        onToggle: () => (this.showHidden = !this.showHidden),
      },
      {
        kind: "check",
        key: "preview",
        label: "Show preview",
        isChecked: this.showPreview,
        onToggle: () => (this.showPreview = !this.showPreview),
      },
      { kind: "divider" },
      {
        kind: "action",
        key: "archive",
        label: "Archive",
        icon: "box",
        isDisabled: true,
        tooltip: "A disabled row keeps its tooltip, to say why",
        onSelect: pick("Archive"),
      },
      { kind: "action", key: "new", label: "New project", icon: "plus", tone: "quiet", onSelect: pick("New project") },
      { kind: "action", key: "delete", label: "Delete", icon: "trash", tone: "danger", onSelect: pick("Delete") },
    ];
  }
}

function buttonsSection(demos: ComponentDemos): CatalogSection {
  return {
    id: "buttons",
    toc: "Buttons",
    title: "Buttons",
    desc: [
      code("m(Button, { variant, sm, icon, round, selected, readonly, quiet, block })"),
      " renders a real button. A dialog's confirming action is primary, a lone dismiss stays secondary, a destructive confirm is destructive.",
    ],
    body: () => [
      demoRow(
        "Variants",
        BUTTON_VARIANTS.map((variant) => m(Button, { variant, id: `button-${variant}` }, variant)),
      ),
      demoRow(
        "On a dark surface",
        m("div", { class: "flex items-center gap-2 rounded-lg bg-inverse p-3" }, [
          m(Button, { variant: "ghost-inverse", id: "button-ghost-inverse" }, "ghost-inverse"),
          m(
            Button,
            { variant: "ghost-inverse", icon: true, "aria-label": "Close" },
            m.trust(icon("close", { size: 16 })),
          ),
        ]),
      ),
      demoRow("Sizes", [
        m(Button, { variant: "primary" }, "Default (34px)"),
        m(Button, { variant: "primary", sm: true }, "Small (28px)"),
        m(Button, { icon: true, "aria-label": "Settings" }, m.trust(icon("settings", { size: 16 }))),
        m(Button, { icon: true, sm: true, "aria-label": "Refresh" }, m.trust(icon("refresh", { size: 14 }))),
        m(
          Button,
          { variant: "ghost", icon: true, xs: true, "aria-label": "Close" },
          m.trust(icon("close", { size: 12 })),
        ),
        m(Button, { round: true }, "Round"),
        m(
          Button,
          { variant: "primary", icon: true, round: true, "aria-label": "Send" },
          m.trust(icon("send", { size: 16 })),
        ),
      ]),
      demoRow("With a glyph", [
        m(Button, { variant: "primary" }, [m.trust(icon("plus", { size: 14 })), "New chat"]),
        m(Button, {}, [m.trust(icon("download", { size: 14 })), "Download"]),
        m(Button, { variant: "ghost" }, [m.trust(icon("external-link", { size: 14 })), "Open"]),
      ]),
      demoRow("States", [
        m(
          Button,
          {
            variant: "ghost",
            icon: true,
            selected: demos.isToggleOn,
            "aria-pressed": demos.isToggleOn ? "true" : "false",
            "aria-label": "Toggle",
            onclick: () => (demos.isToggleOn = !demos.isToggleOn),
            ...hoverTooltipAttrs(demos.isToggleOn ? "Selected (click to turn off)" : "Off (click to turn on)"),
          },
          m.trust(icon("zap", { size: 16 })),
        ),
        m(Button, { selected: true }, "Selected"),
        m(Button, { variant: "primary", disabled: true }, "Disabled"),
        m(
          Button,
          { variant: "primary", readonly: true, ...hoverTooltipAttrs("Read-only keeps its look and explains itself") },
          "Read-only",
        ),
        m(Button, { variant: "ghost", quiet: true }, "Quiet"),
      ]),
      demoRow("Busy", [
        m(Button, { variant: "primary", disabled: true }, [
          m("span", { class: "spinner spinner--sm spinner--current" }),
          "Saving...",
        ]),
      ]),
      demoRow(
        "Block",
        m("div", { class: "w-full max-w-[360px]" }, m(Button, { variant: "primary", block: true }, "Full width")),
      ),
    ],
  };
}

function fieldsSection(demos: ComponentDemos): CatalogSection {
  return {
    id: "fields",
    toc: "Text fields",
    title: "Text fields",
    desc: [
      code("inputClass({ mono, withAction })"),
      " styles an input or a textarea; the accent ring shows on every focus.",
    ],
    body: () =>
      m("div", { class: "grid max-w-[480px] gap-4" }, [
        m("label", { class: "block" }, [
          m("span", { class: MODAL_LABEL_CLASS }, "Text input"),
          m("input", { ...FIELD_PART, id: "field-text", class: inputClass(), placeholder: "you@example.com" }),
        ]),
        m("label", { class: "block" }, [
          m("span", { class: MODAL_LABEL_CLASS }, "Mono"),
          m("input", {
            ...FIELD_PART,
            id: "field-mono",
            class: inputClass({ mono: true }),
            value: "sk-ant-0123456789",
          }),
        ]),
        m("label", { class: "block" }, [
          m("span", { class: MODAL_LABEL_CLASS }, "With an inline action"),
          m("div", { class: "relative" }, [
            m("input", {
              ...FIELD_PART,
              id: "field-action",
              class: inputClass({ withAction: true, mono: true }),
              value: "https://share.example.com/w/abc",
              readonly: true,
            }),
            m(
              "div",
              { class: "absolute inset-y-0 right-1 flex items-center" },
              m(
                Button,
                {
                  sm: true,
                  variant: "ghost",
                  onclick: () => demos.toasts.show("An inline action ran"),
                },
                "Copy",
              ),
            ),
          ]),
        ]),
        m("label", { class: "block" }, [
          m("span", { class: MODAL_LABEL_CLASS }, "Disabled"),
          m("input", {
            ...FIELD_PART,
            id: "field-disabled",
            class: inputClass(),
            value: "Not editable",
            disabled: true,
          }),
        ]),
        m("label", { class: "block" }, [
          m("span", { class: MODAL_LABEL_CLASS }, "Textarea"),
          m(
            "textarea",
            { ...FIELD_PART, id: "field-textarea", class: inputClass({ extra: "resize-y" }), rows: 3 },
            "Some text",
          ),
        ]),
      ]),
  };
}

function pickersSection(demos: ComponentDemos): CatalogSection {
  return {
    id: "pickers",
    toc: "Dropdown & segments",
    title: "Dropdown and segmented control",
    desc: [
      "A dropdown is a form control: picking an option only selects it, never runs an action. A segmented control picks one of a few choices side by side, by label or by glyph.",
    ],
    body: () => [
      demoRow(
        "Dropdown",
        m("div", { id: "demo-dropdown", class: "w-64" }, [
          m(Dropdown<Provider>, {
            options: PROVIDERS,
            value: demos.provider,
            placeholder: "Choose a provider",
            "aria-label": "Provider",
            onSelect: (value) => (demos.provider = value),
          }),
        ]),
      ),
      demoRow("Segmented, by label", [
        m(SegmentedControl<Density>, {
          label: "Density",
          value: demos.density,
          onSelect: (value) => (demos.density = value),
          segments: [
            { value: "comfortable", label: "Comfortable" },
            { value: "compact", label: "Compact" },
            { value: "dense", label: "Dense" },
          ],
        }),
      ]),
      demoRow("Segmented, by glyph", [
        m(SegmentedControl<ViewMode>, {
          label: "View",
          value: demos.viewMode,
          onSelect: (value) => (demos.viewMode = value),
          segments: [
            { value: "grid", label: "Grid", icon: "layout-grid" },
            { value: "list", label: "List", icon: "list" },
            { value: "columns", label: "Columns", icon: "columns" },
          ],
        }),
      ]),
    ],
  };
}

function menuSection(demos: ComponentDemos): CatalogSection {
  return {
    id: "menu",
    toc: "Menu",
    title: "Menu",
    desc: [
      "Every menu is the shared ",
      code("createMenu"),
      ": it opens on a click and closes on a press outside or Escape. A row may open one submenu, on hover; there is no third level. Rows: actions (default, quiet, danger, disabled), a submenu stating its value, a value row, check rows, and dividers.",
    ],
    body: () =>
      m("div", { class: "flex items-center gap-4" }, [
        m(Button, { id: "demo-menu-trigger", ...demos.menu.triggerAttrs() }, [
          "Open the menu",
          m.trust(icon("chevron-down", { size: 14 })),
        ]),
        m("span", { class: "type-helper text-secondary" }, ["Last pick: ", m("strong", demos.lastMenuPick)]),
        demos.menu.view(demos.menuRows()),
      ]),
  };
}

function tooltipsSection(): CatalogSection {
  return {
    id: "tooltips",
    toc: "Tooltips",
    title: "Hover tooltips",
    desc: [
      "Spread ",
      code("hoverTooltipAttrs(text, placement)"),
      " in place of a native title. Centred below by default; above where below has no room; right only for the rail. A ",
      code("detail"),
      " option adds a quieter second line under the text, cut to two lines.",
    ],
    body: () =>
      m("div", { class: "flex flex-wrap items-center gap-3" }, [
        m(Button, { ...hoverTooltipAttrs("Centred below") }, "Below"),
        m(Button, { ...hoverTooltipAttrs("Above the control", "above") }, "Above"),
        m(Button, { ...hoverTooltipAttrs("To the right", "right") }, "Right"),
        m(
          Button,
          {
            ...hoverTooltipAttrs(
              "A narrower bubble for a sentence or two of explanation, which reads better wrapped than in one long line.",
              "below",
              { maxWidth: 240 },
            ),
          },
          "Long text",
        ),
        m(
          Button,
          {
            id: "tooltip-with-detail",
            ...hoverTooltipAttrs("Open in Files", "below", { detail: "data/reports/q4-summary.md" }),
          },
          "With a detail line",
        ),
      ]),
  };
}

function badgesSection(): CatalogSection {
  return {
    id: "badges",
    toc: "Badges",
    title: "Badges",
    desc: [code("badgeClass(tone, { mono })"), " on a span: a small label chip, outlined or tinted by tone."],
    body: () => [
      demoRow(
        "Tones",
        BADGE_TONES.map((tone) => m("span", { ...BADGE_PART, id: `badge-${tone}`, class: badgeClass(tone) }, tone)),
      ),
      demoRow("Mono, for an id", [
        m("span", { ...BADGE_PART, class: badgeClass("neutral", { mono: true }) }, "claude-sonnet-5-5"),
        m("span", { ...BADGE_PART, class: badgeClass("accent", { mono: true }) }, "agent-7f3a"),
      ]),
      demoRow("With a glyph", [
        m("span", { ...BADGE_PART, class: badgeClass("success") }, [
          m.trust(icon("check", { size: 12 })),
          "Connected",
        ]),
        m("span", { ...BADGE_PART, class: badgeClass("danger") }, [m.trust(icon("alert", { size: 12 })), "Failing"]),
      ]),
    ],
  };
}

function statusSection(): CatalogSection {
  return {
    id: "status",
    toc: "Banners & activity",
    title: "Banners, activity dots and spinners",
    desc: [
      code("bannerClass(marker, tone)"),
      " is the full-width strip above a pane's content; ",
      code("activityDotClass"),
      " the pulsing working dot; ",
      code(".spinner"),
      " the one ring spinner.",
    ],
    body: () => [
      demoRow(
        "Banners",
        m("div", { class: "flex w-full max-w-[560px] flex-col gap-2" }, [
          m("div", { id: "banner-neutral", class: `${bannerClass("demo-banner", "neutral")} rounded-md border` }, [
            m("span", "This chat is read-only."),
            m(Button, { sm: true, variant: "ghost" }, "Dismiss"),
          ]),
          m("div", { id: "banner-warning", class: `${bannerClass("demo-banner", "warning")} rounded-md border` }, [
            m("span", "The workspace is running low on memory."),
            m(Button, { sm: true }, "See what is using it"),
          ]),
        ]),
      ),
      demoRow("Activity dots", [
        m("span", { class: "flex items-center gap-2 type-body text-secondary" }, [
          m("span", { class: activityDotClass("h-2 w-2") }),
          "Working",
        ]),
        m("span", { class: "flex items-center gap-2 type-body text-secondary" }, [
          m("span", { class: activityDotClass("h-2 w-2", "warning") }),
          "Waiting to come up",
        ]),
      ]),
      demoRow("Spinners", [
        m("span", { class: "spinner" }),
        m("span", { class: "spinner spinner--sm" }),
        m("span", { class: "text-accent" }, m.trust(spinnerIcon(24))),
        m("span", { class: "flex items-center gap-2 rounded-md bg-inverse px-3 py-2 text-on-accent type-body" }, [
          m("span", { class: "spinner spinner--sm spinner--current" }),
          "On a coloured surface",
        ]),
      ]),
    ],
  };
}

function overlaysSection(demos: ComponentDemos): CatalogSection {
  return {
    id: "dialogs",
    toc: "Dialogs & toasts",
    title: "Dialogs and toasts",
    desc: [
      "The shared ",
      code("Modal"),
      " shell, the notice dialog every notice uses, the confirm dialog for an irreversible verb, and the toasts that report a failed operation without stopping everything.",
    ],
    body: () => [
      m("div", { class: "flex flex-wrap gap-3" }, [
        m(Button, { id: "open-modal", onclick: () => (demos.openDialog = "modal") }, "Open a modal"),
        m(Button, { id: "open-notice", onclick: () => (demos.openDialog = "notice") }, "Open a notice"),
        m(
          Button,
          { id: "open-destroy", variant: "ghost-destructive", onclick: () => (demos.openDialog = "destroy") },
          "Open a delete confirmation",
        ),
        m(
          Button,
          { id: "show-toast", onclick: () => demos.toasts.show("Couldn't save the file: the disk is full.") },
          "Show a toast",
        ),
      ]),
      demos.openDialog === "modal"
        ? m(
            Modal,
            {
              title: "Rename project",
              onDismiss: () => (demos.openDialog = null),
              onEscape: () => (demos.openDialog = null),
              actions: [
                m(Button, { onclick: () => (demos.openDialog = null) }, "Cancel"),
                m(Button, { variant: "primary", onclick: () => (demos.openDialog = null) }, "Rename"),
              ],
            },
            [
              m(
                "p",
                { class: MODAL_MESSAGE_CLASS },
                "A modal: a dimmed backdrop, a centred card, a title and actions.",
              ),
              m("label", { class: "mb-4 block" }, [
                m("span", { class: MODAL_LABEL_CLASS }, "Name"),
                m("input", { ...FIELD_PART, class: inputClass(), value: "Quarterly report" }),
              ]),
            ],
          )
        : null,
      demos.openDialog === "notice"
        ? m(demos.noticeDialog, {
            title: "Couldn't send the message",
            body: ["The chat app didn't answer.", "Your message is still in the composer."],
            dismissLabel: "Cancel",
            actions: [{ label: "Try again", run: () => (demos.openDialog = null) }],
            onDismiss: () => (demos.openDialog = null),
          })
        : null,
      demos.openDialog === "destroy"
        ? m(DestroyConfirmDialog, {
            agentName: "Quarterly report",
            onConfirm: () => (demos.openDialog = null),
            onCancel: () => (demos.openDialog = null),
          })
        : null,
      m(Toasts, {
        toasts: demos.toasts.current(),
        placementClass: "fixed bottom-6",
        onDismiss: (id) => demos.toasts.dismiss(id),
      }),
    ],
  };
}

function surfacesSection(): CatalogSection {
  return {
    id: "surfaces",
    toc: "Surfaces",
    title: "Surfaces in use",
    desc: [
      "How the surface tokens stack: the page, a sidebar beside it, a card on the page, and the hover and pressed fills, which are translucent so they deepen over a tinted row.",
    ],
    body: () =>
      m("div", { class: "flex h-56 overflow-hidden rounded-lg border border-default" }, [
        m("div", { id: "surface-sidebar", class: "flex w-48 flex-col gap-0.5 bg-sidebar p-2" }, [
          m(
            "div",
            { class: "rounded-md px-2 py-1.5 text-(length:--font-size-row) text-primary hover:bg-fill-hover" },
            "A row (hover me)",
          ),
          m(
            "div",
            { class: "rounded-md bg-fill-active px-2 py-1.5 text-(length:--font-size-row) text-primary" },
            "The current row",
          ),
          m(
            "div",
            { class: "rounded-md px-2 py-1.5 text-(length:--font-size-row) text-secondary hover:bg-fill-hover" },
            "Another row",
          ),
          caption("bg-sidebar", "mt-auto"),
        ]),
        m("div", { id: "surface-page", class: "flex flex-1 flex-col gap-3 border-l border-default bg-page p-4" }, [
          m("div", { class: "rounded-lg border border-default bg-surface p-4 shadow-raised" }, [
            m("p", { class: "type-label text-primary" }, "A card"),
            m("p", { class: "type-helper text-secondary" }, "bg-surface, border-default, shadow-raised"),
          ]),
          m(
            "div",
            { class: "rounded-lg bg-surface-secondary p-3 type-helper text-secondary" },
            "bg-surface-secondary",
          ),
          m("div", { class: "rounded-lg bg-code p-3 font-mono type-helper text-primary" }, "bg-code: const x = 1;"),
          caption("bg-page", "mt-auto"),
        ]),
      ]),
  };
}

export function componentsGroup(demos: ComponentDemos): CatalogGroup {
  return {
    id: "components",
    label: "Components",
    description:
      "The shared controls built from those tokens, drawn by the library itself; the interactive ones work.",
    sections: [
      buttonsSection(demos),
      fieldsSection(demos),
      pickersSection(demos),
      menuSection(demos),
      tooltipsSection(),
      badgesSection(),
      statusSection(),
      overlaysSection(demos),
      surfacesSection(),
    ],
  };
}
