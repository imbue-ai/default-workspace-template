/**
 * The Keyboard settings section (user-profile-state spec, section 10.3): every app's actions from their
 * manifests, grouped by app, each with its chords drawn as keycaps. It is the body of the Keyboard shortcuts
 * dialog (opened from the backdrop's context menu) and of the `/keyboard-shortcuts` page that Imbue Studio's
 * Workspace settings frames.
 *
 * Editing one action opens its editor below the row: the chords it will have, each removable, a recorder that
 * adds one more, and the save surfaces. A chord another action of the same app already uses is offered to be
 * moved here (Replace) or not (Cancel); a chord Imbue Studio or the browser keeps for itself is saved with a
 * warning. The owner saves "only in this workspace" (the override file) or "everywhere" (the account, through
 * the delivery flow); a visitor's save is always their own override here. An "everywhere" save the account has
 * not taken yet shows as waiting, with retry and discard.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { chordFromKeydown, chordSpokenLabel, isApplePlatform } from "@imbue/workspace-ui/src/keybindings";
import {
  type KeybindingActionView,
  type KeybindingAppView,
  type KeybindingsCatalog,
  type KeybindingsDeliveryOutcome,
  type UnsentKeybindingView,
  deliverKeybindings,
  discardKeybinding,
  fetchKeybindingsCatalog,
  resetKeybinding,
  saveKeybinding,
} from "../model/api";
import { ChordKeycaps } from "./ChordKeycaps";

/** The other action of the same app already using the chord, if any (conflicts are per app). */
export function conflictingAction(
  app: KeybindingAppView,
  actionId: string,
  chord: string,
  chordsByActionId: ReadonlyMap<string, readonly string[]> = new Map(),
): KeybindingActionView | null {
  return (
    app.actions.find(
      (action) => action.id !== actionId && (chordsByActionId.get(action.id) ?? action.chords).includes(chord),
    ) ?? null
  );
}

// Chords Imbue Studio (its menus and the keys it relays itself), the browser a share link opens in, or the
// operating system take before a page sees them.
const RESERVED_APPLE_CHORDS: ReadonlySet<string> = new Set([
  "meta+w",
  "meta+q",
  "meta+n",
  "meta+t",
  "meta+h",
  "meta+m",
  "meta+l",
  "meta+r",
  "meta+tab",
  "meta+space",
  "meta+=",
  "meta+-",
  "meta+0",
  "alt+meta+i",
  "ctrl+meta+f",
]);
const RESERVED_OTHER_CHORDS: ReadonlySet<string> = new Set([
  "ctrl+w",
  "ctrl+q",
  "ctrl+n",
  "ctrl+t",
  "ctrl+l",
  "ctrl+r",
  "ctrl+tab",
  "ctrl+shift+tab",
  "ctrl+shift+c",
  "ctrl+=",
  "ctrl+-",
  "ctrl+0",
  "alt+f4",
  "f5",
  "f11",
]);

/** Why a chord may never reach the workspace, or null when nothing outside the workspace takes it. */
export function reservedChordNotice(chord: string, isApple: boolean): string | null {
  const reserved = isApple ? RESERVED_APPLE_CHORDS : RESERVED_OTHER_CHORDS;
  if (!reserved.has(chord)) return null;
  return `${chordSpokenLabel(chord, isApple)} is used by Imbue Studio or your browser, so it may not reach the workspace.`;
}

/** An unsent save named the way the list names it: its app's display name and its action's label. */
export function unsentEntryLabel(loaded: KeybindingsCatalog, entry: UnsentKeybindingView): string {
  const appView = loaded.apps.find((candidate) => candidate.app === entry.app);
  const action = appView?.actions.find((candidate) => candidate.id === entry.action);
  return `${appView?.display_name ?? entry.app}: ${action?.label ?? entry.action}`;
}

export function areChordListsEqual(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((chord, index) => chord === right[index]);
}

// Where a changed action's chords come from, said on hover over its "Changed" marker.
const CHANGED_SOURCE_DESCRIPTIONS: Record<string, string> = {
  workspace: "Changed for you in this workspace",
  unsent: "Changed here; waiting to be saved to your account",
  account: "Changed for your account, in every workspace",
};

/** A recorded chord another action of the same app already uses, until the person replaces or cancels. */
interface PendingClash {
  readonly chord: string;
  readonly otherAction: KeybindingActionView;
}

/** The one action being edited: the chords it will have, and the chords Replace left its app's other actions. */
interface ActionEditor {
  readonly app: KeybindingAppView;
  readonly action: KeybindingActionView;
  stagedChords: string[];
  readonly replacedChordsByActionId: Map<string, string[]>;
  isRecording: boolean;
  clash: PendingClash | null;
}

export interface KeyboardSettingsSectionAttrs {
  /** Whether chords read with the Mac's symbols; the browser's platform when omitted. */
  readonly isApple?: boolean;
}

const SECTION_HEADER_CLASS =
  "keybinding-app-header flex w-full items-center gap-2 rounded-md px-1 py-1.5 text-left hover:bg-fill-hover";

export function KeyboardSettingsSection(): m.Component<KeyboardSettingsSectionAttrs> {
  let catalog: KeybindingsCatalog | null = null;
  let loadError: string | null = null;
  let editor: ActionEditor | null = null;
  let notice: string | null = null;
  let isBusy = false;
  const collapsedApps = new Set<string>();

  async function load(): Promise<void> {
    try {
      catalog = await fetchKeybindingsCatalog();
      loadError = null;
    } catch (error) {
      loadError = (error as Error).message;
    }
    m.redraw();
  }

  function startEditing(app: KeybindingAppView, action: KeybindingActionView): void {
    editor = {
      app,
      action,
      stagedChords: [...action.chords],
      replacedChordsByActionId: new Map(),
      isRecording: false,
      clash: null,
    };
    notice = null;
  }

  function hasUnsavedChanges(current: ActionEditor): boolean {
    return (
      !areChordListsEqual(current.stagedChords, current.action.chords) || current.replacedChordsByActionId.size > 0
    );
  }

  function addChord(current: ActionEditor, chord: string): void {
    if (!current.stagedChords.includes(chord)) current.stagedChords = [...current.stagedChords, chord];
    current.isRecording = false;
    current.clash = null;
  }

  function onRecorderKeydown(current: ActionEditor, event: KeyboardEvent): void {
    event.preventDefault();
    event.stopPropagation();
    if (event.key === "Escape") {
      current.isRecording = false;
      return;
    }
    const chord = chordFromKeydown(event);
    if (chord === null) return;
    const other = conflictingAction(current.app, current.action.id, chord, current.replacedChordsByActionId);
    if (other === null) addChord(current, chord);
    else current.clash = { chord, otherAction: other };
  }

  function replaceClash(current: ActionEditor, clash: PendingClash): void {
    const otherChords = current.replacedChordsByActionId.get(clash.otherAction.id) ?? [...clash.otherAction.chords];
    current.replacedChordsByActionId.set(
      clash.otherAction.id,
      otherChords.filter((chord) => chord !== clash.chord),
    );
    addChord(current, clash.chord);
  }

  /** What a delivery attempt leaves on screen: nothing once it landed, else its own word on why not. */
  function noticeOfDelivery(delivery: KeybindingsDeliveryOutcome | null): string | null {
    return delivery === null || delivery.status === "DELIVERED" ? null : delivery.detail;
  }

  async function save(current: ActionEditor, scope: "workspace" | "account"): Promise<void> {
    isBusy = true;
    try {
      let lastDelivery: KeybindingsDeliveryOutcome | null = null;
      for (const [otherActionId, otherChords] of current.replacedChordsByActionId) {
        lastDelivery = (await saveKeybinding(current.app.app, otherActionId, otherChords, scope)).delivery;
      }
      lastDelivery = (await saveKeybinding(current.app.app, current.action.id, current.stagedChords, scope)).delivery;
      notice = noticeOfDelivery(lastDelivery);
      editor = null;
      await load();
    } catch (error) {
      notice = (error as Error).message;
    }
    isBusy = false;
    m.redraw();
  }

  async function act(run: () => Promise<unknown>): Promise<void> {
    isBusy = true;
    try {
      await run();
      await load();
    } catch (error) {
      notice = (error as Error).message;
    }
    isBusy = false;
    m.redraw();
  }

  function chordsView(chords: readonly string[], isApple: boolean): m.Children {
    if (chords.length === 0) return m("span", { class: "keybinding-off type-helper text-faint" }, "Off");
    return m(
      "span",
      { class: "flex flex-wrap items-center justify-end gap-1.5" },
      chords.map((chord) => m(ChordKeycaps, { key: chord, chord, isApple })),
    );
  }

  function clashView(current: ActionEditor, clash: PendingClash, isApple: boolean): m.Children {
    return m("div", { class: "keybinding-clash mt-2 rounded-md bg-warning-surface p-2" }, [
      m(
        "p",
        { class: "type-helper text-primary" },
        `${chordSpokenLabel(clash.chord, isApple)} already runs "${clash.otherAction.label}".`,
      ),
      m("div", { class: "mt-1.5 flex gap-2" }, [
        m(Button, { sm: true, extra: "keybinding-replace", onclick: () => replaceClash(current, clash) }, "Replace"),
        m(
          Button,
          {
            sm: true,
            variant: "ghost",
            extra: "keybinding-clash-cancel",
            onclick: () => {
              current.clash = null;
              current.isRecording = false;
            },
          },
          "Cancel",
        ),
      ]),
    ]);
  }

  function stagedChordsView(current: ActionEditor, isApple: boolean): m.Children {
    return m("div", { class: "flex flex-wrap items-center gap-2" }, [
      current.stagedChords.length === 0
        ? m("span", { class: "type-helper text-faint" }, "Off: no shortcut runs this")
        : current.stagedChords.map((chord) =>
            m("span", { key: chord, class: "keybinding-staged inline-flex items-center gap-0.5" }, [
              m(ChordKeycaps, { chord, isApple }),
              m(
                Button,
                {
                  xs: true,
                  icon: true,
                  variant: "ghost",
                  extra: "keybinding-remove",
                  "aria-label": `Remove ${chordSpokenLabel(chord, isApple)}`,
                  ...hoverTooltipAttrs(`Remove ${chordSpokenLabel(chord, isApple)}`),
                  onclick: () => {
                    current.stagedChords = current.stagedChords.filter((staged) => staged !== chord);
                  },
                },
                m.trust(icon("close", { size: 12 })),
              ),
            ]),
          ),
      current.isRecording
        ? null
        : m(
            Button,
            {
              sm: true,
              variant: "ghost",
              extra: "keybinding-add",
              disabled: current.clash !== null,
              onclick: () => {
                current.isRecording = true;
              },
            },
            [m.trust(icon("plus", { size: 12 })), " Add shortcut"],
          ),
    ]);
  }

  function recorderView(current: ActionEditor): m.Children {
    if (!current.isRecording || current.clash !== null) return null;
    return m("input", {
      class:
        "keybinding-recorder type-body mt-2 w-full rounded-md border border-accent bg-page px-2 py-1 outline-none",
      readonly: true,
      placeholder: "Press the new shortcut (Esc to cancel)",
      "aria-label": "Press the new shortcut",
      oncreate: (created: m.VnodeDOM) => (created.dom as HTMLInputElement).focus(),
      onkeydown: (event: KeyboardEvent) => onRecorderKeydown(current, event),
      onblur: () => {
        current.isRecording = false;
      },
    });
  }

  function saveButtons(current: ActionEditor): m.Children {
    const isOwner = catalog?.is_owner === true;
    const canEverywhere = catalog?.can_save_everywhere === true;
    const isSaveBlocked = isBusy || current.isRecording || current.clash !== null || !hasUnsavedChanges(current);
    return m("div", { class: "mt-3 flex flex-wrap items-center gap-2" }, [
      m(
        Button,
        { sm: true, variant: "primary", disabled: isSaveBlocked, onclick: () => void save(current, "workspace") },
        isOwner ? "Only in this workspace" : "Save",
      ),
      isOwner
        ? m(
            Button,
            {
              sm: true,
              disabled: isSaveBlocked || !canEverywhere,
              ...hoverTooltipAttrs(
                canEverywhere
                  ? "Save for your account, in every workspace"
                  : "Saving everywhere needs Imbue Studio, which this workspace is not connected to.",
              ),
              onclick: () => void save(current, "account"),
            },
            "Everywhere",
          )
        : null,
      m(
        Button,
        {
          sm: true,
          variant: "ghost",
          extra: "keybinding-cancel",
          onclick: () => {
            editor = null;
          },
        },
        "Cancel",
      ),
      areChordListsEqual(current.stagedChords, current.action.default_chords)
        ? null
        : m(
            Button,
            {
              sm: true,
              variant: "ghost",
              extra: "keybinding-restore-default ml-auto",
              onclick: () => {
                current.stagedChords = [...current.action.default_chords];
              },
            },
            "Restore default",
          ),
    ]);
  }

  function editorView(current: ActionEditor, isApple: boolean): m.Children {
    const reservedNotices = current.stagedChords
      .map((chord) => reservedChordNotice(chord, isApple))
      .filter((reserved): reserved is string => reserved !== null);
    return m("div", { class: "keybinding-editor mt-2 mb-3 rounded-md border border-default bg-surface p-3" }, [
      stagedChordsView(current, isApple),
      recorderView(current),
      current.clash === null ? null : clashView(current, current.clash, isApple),
      ...reservedNotices.map((reserved) =>
        m("p", { class: "keybinding-reserved type-helper mt-2 text-warning" }, reserved),
      ),
      catalog?.is_owner === true && catalog.can_save_everywhere !== true
        ? m(
            "p",
            { class: "type-helper mt-2 text-secondary" },
            "Shortcuts saved here apply only in this workspace: saving to your account needs Imbue Studio.",
          )
        : null,
      saveButtons(current),
    ]);
  }

  function actionRow(appView: KeybindingAppView, action: KeybindingActionView, isApple: boolean): m.Children {
    const isEditing = editor !== null && editor.app.app === appView.app && editor.action.id === action.id;
    const changedDescription = action.source === "default" ? null : CHANGED_SOURCE_DESCRIPTIONS[action.source];
    return m("div", { key: action.id, "data-keybinding-action": `${appView.app}.${action.id}` }, [
      m("div", { class: "flex min-h-9 items-center gap-3 border-b border-default py-1.5" }, [
        m("span", { class: "type-body min-w-0 flex-1 text-primary" }, action.label),
        changedDescription === undefined || changedDescription === null
          ? null
          : m(
              "span",
              {
                class: "keybinding-changed inline-flex shrink-0 items-center gap-1 type-helper text-accent",
                ...hoverTooltipAttrs(changedDescription),
              },
              [m("span", { class: "h-1.5 w-1.5 rounded-full bg-accent" }), "Changed"],
            ),
        m("span", { class: "shrink-0" }, chordsView(action.chords, isApple)),
        m("span", { class: "flex shrink-0 items-center gap-1" }, [
          action.source === "workspace"
            ? m(
                Button,
                {
                  sm: true,
                  variant: "ghost",
                  extra: "keybinding-reset",
                  disabled: isBusy,
                  ...hoverTooltipAttrs("Undo your change in this workspace"),
                  onclick: () => void act(() => resetKeybinding(appView.app, action.id)),
                },
                "Reset",
              )
            : null,
          m(
            Button,
            {
              sm: true,
              variant: "ghost",
              extra: "keybinding-edit",
              disabled: isBusy || isEditing,
              onclick: () => startEditing(appView, action),
            },
            "Edit",
          ),
        ]),
      ]),
      isEditing && editor !== null ? editorView(editor, isApple) : null,
    ]);
  }

  function appSection(appView: KeybindingAppView, isApple: boolean): m.Children {
    const isCollapsed = collapsedApps.has(appView.app);
    const count = appView.actions.length;
    return m("section", { key: appView.app, class: "mb-4", "data-keybinding-app": appView.app }, [
      m(
        "button",
        {
          type: "button",
          class: SECTION_HEADER_CLASS,
          "aria-expanded": isCollapsed ? "false" : "true",
          onclick: () => {
            if (isCollapsed) collapsedApps.delete(appView.app);
            else collapsedApps.add(appView.app);
          },
        },
        [
          m(
            "span",
            { class: "text-secondary" },
            m.trust(icon(isCollapsed ? "chevron-right" : "chevron-down", { size: 14 })),
          ),
          m("span", { class: "type-label text-primary" }, appView.display_name),
          m("span", { class: "type-helper text-faint" }, `${count} ${count === 1 ? "shortcut" : "shortcuts"}`),
        ],
      ),
      isCollapsed
        ? null
        : m(
            "div",
            { class: "pl-6" },
            appView.actions.map((action) => actionRow(appView, action, isApple)),
          ),
    ]);
  }

  function unsentBanner(loaded: KeybindingsCatalog): m.Children {
    if (loaded.unsent.length === 0) return null;
    return m("div", { class: "mb-4 rounded-md bg-warning-surface p-3", "data-keybinding-unsent": true }, [
      m(
        "p",
        { class: "type-helper text-primary" },
        loaded.has_pending_permission
          ? "Waiting for your approval in Imbue Studio; these apply in this workspace meanwhile."
          : "Not yet saved to your account; these apply in this workspace meanwhile.",
      ),
      ...loaded.unsent.map((entry) =>
        m("div", { class: "mt-1 flex items-center justify-between gap-2" }, [
          m("span", { class: "type-helper min-w-0 truncate text-secondary" }, unsentEntryLabel(loaded, entry)),
          m(
            Button,
            {
              sm: true,
              variant: "ghost",
              disabled: isBusy,
              onclick: () => void act(() => discardKeybinding(entry.app, entry.action)),
            },
            "Discard",
          ),
        ]),
      ),
      m(
        Button,
        {
          sm: true,
          extra: "mt-2",
          disabled: isBusy,
          onclick: () =>
            void act(async () => {
              notice = noticeOfDelivery(await deliverKeybindings([]));
            }),
        },
        "Try saving to your account again",
      ),
    ]);
  }

  return {
    oninit() {
      void load();
    },
    view(vnode) {
      if (loadError !== null) {
        return m("p", { class: "type-helper text-danger" }, `The shortcuts could not be loaded: ${loadError}`);
      }
      if (catalog === null) return m("p", { class: "type-helper text-secondary" }, "Loading the shortcuts...");
      const isApple = vnode.attrs.isApple ?? isApplePlatform();
      const loaded = catalog;
      return m("div", { "data-keyboard-settings": true }, [
        unsentBanner(loaded),
        notice !== null ? m("p", { class: "keybinding-notice type-helper mb-3 text-secondary" }, notice) : null,
        m(
          "div",
          { class: "keybinding-apps" },
          loaded.apps.filter((appView) => appView.actions.length > 0).map((appView) => appSection(appView, isApple)),
        ),
      ]);
    },
  };
}
