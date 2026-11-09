/**
 * The Keyboard settings section (user-profile-state spec, section 10.3): every app's actions from their
 * manifests, grouped by app, each with its shortcuts drawn as keycaps. It is the body of the `/keyboard-shortcuts`
 * page that Imbue Studio's Workspace settings frames, and of the Keyboard shortcuts dialog a plain browser opens
 * from the backdrop's context menu.
 *
 * Editing one action opens its editor below the row: the shortcuts it will have, each removable, a recorder that
 * adds one more, and the save surfaces. The recorder takes every key, Escape included (it has a Cancel of its own),
 * and says when it is listening, so whatever frames it can leave those keys alone.
 *
 * No step leaves one shortcut on two actions of an app without asking: recording a shortcut another action uses,
 * restoring the defaults, and resetting a change each offer Replace (take it from the other action, saved together)
 * or Cancel when what they would bring in is in use. A replaced shortcut leaves the other action's row at once,
 * marked as changing on save; a shortcut that still ends up on two actions (from layers this screen does not edit)
 * is flagged on both rows. A shortcut Imbue Studio or the browser keeps for itself is saved with a warning.
 *
 * The owner saves "only in this workspace" (the override file) or "everywhere" (the account, through the delivery
 * flow); a visitor's save is always their own override here. A saved or reset action is marked as such until the
 * section is opened again. An "everywhere" save the account has not taken yet shows as waiting, with retry and
 * discard.
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
import { setShortcutRecording } from "../model/shortcutRecording";
import { ShortcutKeycap } from "./ShortcutKeycap";

/** One shortcut a step would bring to an action that another action of the app uses now. */
export interface ClashEntry {
  readonly chord: string;
  readonly otherAction: KeybindingActionView;
}

/** Every other action of the app using one of `chords` now, once per chord it uses (conflicts are per app). An
 *  action's chords are read from `chordsByActionId` ahead of what it was loaded with, for edits not yet saved. */
export function clashesOf(
  app: KeybindingAppView,
  actionId: string,
  chords: readonly string[],
  chordsByActionId: ReadonlyMap<string, readonly string[]> = new Map(),
): ClashEntry[] {
  return chords.flatMap((chord) =>
    app.actions
      .filter((action) => action.id !== actionId && (chordsByActionId.get(action.id) ?? action.chords).includes(chord))
      .map((otherAction) => ({ chord, otherAction })),
  );
}

/** The other actions of the app sharing one of this action's shortcuts, by the chords each row shows. */
export function actionsSharingShortcuts(
  app: KeybindingAppView,
  actionId: string,
  chordsOfAction: (action: KeybindingActionView) => readonly string[],
): KeybindingActionView[] {
  const own = app.actions.find((action) => action.id === actionId);
  if (own === undefined) return [];
  const ownChords = chordsOfAction(own);
  return app.actions.filter(
    (action) => action.id !== actionId && chordsOfAction(action).some((chord) => ownChords.includes(chord)),
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

/** Whether a reset would change what the action runs: it has a change in this workspace, and that change differs
 *  from what removing it restores (a Replace elsewhere can leave a change equal to it). */
export function isResetOffered(action: KeybindingActionView): boolean {
  if (action.source !== "workspace") return false;
  const restored = new Set(action.reset_chords);
  return action.chords.length !== restored.size || action.chords.some((chord) => !restored.has(chord));
}

/** What brought shortcuts in use elsewhere into an action: a recorded one, its defaults, a reset of this
 *  workspace's change, or a discard of an undelivered "everywhere" save. */
type ClashCause = "record" | "restore-default" | "reset" | "discard";

/** Shortcuts a step would bring in that other actions use, until the person replaces or cancels. */
interface PendingClash {
  readonly cause: ClashCause;
  readonly entries: readonly ClashEntry[];
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

/** A reset or a discard whose restored shortcuts other actions use, waiting on Replace or Cancel. */
interface PendingRevert {
  readonly app: KeybindingAppView;
  readonly action: KeybindingActionView;
  readonly clash: PendingClash;
}

export interface KeyboardSettingsSectionAttrs {
  /** Whether chords read with the Mac's symbols; the browser's platform when omitted. */
  readonly isApple?: boolean;
  /** Told each time the recorder starts or stops listening for a new shortcut. */
  readonly onRecordingChange?: (isRecording: boolean) => void;
}

const SECTION_HEADER_CLASS =
  "keybinding-app-header flex w-full items-center gap-2 rounded-md px-1 py-1.5 text-left hover:bg-fill-hover";

function actionKey(app: string, actionId: string): string {
  return `${app}.${actionId}`;
}

/** The sentence a clash box opens with: which shortcut, what has it now, and what the step would do with it. */
function clashSentence(cause: ClashCause, entry: ClashEntry, isApple: boolean): string {
  const shortcut = chordSpokenLabel(entry.chord, isApple);
  if (cause === "record") return `${shortcut} already runs "${entry.otherAction.label}".`;
  if (cause === "restore-default") {
    return `The default includes ${shortcut}, which "${entry.otherAction.label}" uses now.`;
  }
  const step = cause === "reset" ? "Resetting" : "Discarding";
  return `${step} brings back ${shortcut}, which "${entry.otherAction.label}" uses now.`;
}

export function KeyboardSettingsSection(): m.Component<KeyboardSettingsSectionAttrs> {
  let catalog: KeybindingsCatalog | null = null;
  let loadError: string | null = null;
  let editor: ActionEditor | null = null;
  let pendingRevert: PendingRevert | null = null;
  let notice: string | null = null;
  let isBusy = false;
  let isRecorderListening = false;
  let onRecordingChange: ((isRecording: boolean) => void) | undefined;
  const collapsedApps = new Set<string>();
  // The actions saved or reset since this section opened, marked until it is opened again.
  const savedActionKeys = new Set<string>();

  async function load(): Promise<void> {
    try {
      catalog = await fetchKeybindingsCatalog();
      loadError = null;
    } catch (error) {
      loadError = (error as Error).message;
    }
    m.redraw();
  }

  /** Start or stop the recorder, telling the shell and whatever frames this section when that changes. */
  function setRecording(current: ActionEditor | null, isRecording: boolean): void {
    if (current !== null) current.isRecording = isRecording;
    if (isRecording === isRecorderListening) return;
    isRecorderListening = isRecording;
    setShortcutRecording(isRecording);
    onRecordingChange?.(isRecording);
  }

  function closeEditor(): void {
    setRecording(editor, false);
    editor = null;
  }

  function startEditing(app: KeybindingAppView, action: KeybindingActionView): void {
    closeEditor();
    pendingRevert = null;
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
  }

  function onRecorderKeydown(current: ActionEditor, event: KeyboardEvent): void {
    event.preventDefault();
    event.stopPropagation();
    const chord = chordFromKeydown(event);
    if (chord === null) return;
    const entries = clashesOf(current.app, current.action.id, [chord], current.replacedChordsByActionId);
    if (entries.length === 0) addChord(current, chord);
    else current.clash = { cause: "record", entries };
    setRecording(current, false);
  }

  function restoreDefaults(current: ActionEditor): void {
    const entries = clashesOf(
      current.app,
      current.action.id,
      current.action.default_chords,
      current.replacedChordsByActionId,
    );
    if (entries.length === 0) current.stagedChords = [...current.action.default_chords];
    else current.clash = { cause: "restore-default", entries };
  }

  /** Take every clashing shortcut off the action that has it now (saved with the edit), then finish the step. */
  function replaceClash(current: ActionEditor, clash: PendingClash): void {
    for (const entry of clash.entries) {
      const otherChords = current.replacedChordsByActionId.get(entry.otherAction.id) ?? [...entry.otherAction.chords];
      current.replacedChordsByActionId.set(
        entry.otherAction.id,
        otherChords.filter((chord) => chord !== entry.chord),
      );
    }
    if (clash.cause === "restore-default") current.stagedChords = [...current.action.default_chords];
    else for (const entry of clash.entries) addChord(current, entry.chord);
    current.clash = null;
  }

  /** The chords a row shows: what is saved, or what Replace has left another action of the app being edited. */
  function shownChords(appView: KeybindingAppView, action: KeybindingActionView): readonly string[] {
    if (editor === null || editor.app.app !== appView.app) return action.chords;
    return editor.replacedChordsByActionId.get(action.id) ?? action.chords;
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
        savedActionKeys.add(actionKey(current.app.app, otherActionId));
      }
      lastDelivery = (await saveKeybinding(current.app.app, current.action.id, current.stagedChords, scope)).delivery;
      savedActionKeys.add(actionKey(current.app.app, current.action.id));
      notice = noticeOfDelivery(lastDelivery);
      closeEditor();
      await load();
    } catch (error) {
      // Some saves may have landed before the one that failed: show what the server holds now.
      notice = (error as Error).message;
      closeEditor();
      await load();
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
      await load();
    }
    isBusy = false;
    m.redraw();
  }

  /** Undo a change (a reset of this workspace's, or a discard of an unsent "everywhere" save), asking first when
   *  what it brings back is another action's now. */
  function revert(
    appView: KeybindingAppView,
    action: KeybindingActionView,
    cause: "reset" | "discard",
    restoredChords: readonly string[],
  ): void {
    const entries = clashesOf(appView, action.id, restoredChords);
    if (entries.length > 0) {
      pendingRevert = { app: appView, action, clash: { cause, entries } };
      return;
    }
    void act(() => runRevert(appView.app, action.id, cause));
  }

  async function runRevert(app: string, actionId: string, cause: ClashCause): Promise<void> {
    if (cause === "discard") await discardKeybinding(app, actionId);
    else await resetKeybinding(app, actionId);
    savedActionKeys.add(actionKey(app, actionId));
  }

  /** Take the restored shortcuts off the actions that have them now, in this workspace, then revert. */
  function replaceAndRevert(current: PendingRevert): void {
    pendingRevert = null;
    void act(async () => {
      const remainingChordsByActionId = new Map<string, string[]>();
      for (const entry of current.clash.entries) {
        const otherChords = remainingChordsByActionId.get(entry.otherAction.id) ?? [...entry.otherAction.chords];
        remainingChordsByActionId.set(
          entry.otherAction.id,
          otherChords.filter((chord) => chord !== entry.chord),
        );
      }
      for (const [otherActionId, otherChords] of remainingChordsByActionId) {
        await saveKeybinding(current.app.app, otherActionId, otherChords, "workspace");
        savedActionKeys.add(actionKey(current.app.app, otherActionId));
      }
      await runRevert(current.app.app, current.action.id, current.clash.cause);
    });
  }

  /** A pending revert's Replace / Cancel box, where it was asked for. */
  function pendingRevertView(current: PendingRevert, isApple: boolean): m.Children {
    return m(
      "div",
      { class: "mb-3" },
      clashView(
        current.clash,
        isApple,
        () => replaceAndRevert(current),
        () => {
          pendingRevert = null;
        },
      ),
    );
  }

  /** Each shortcut its own keycap, with "or" between the alternatives. */
  function shortcutsView(chords: readonly string[], isApple: boolean): m.Children {
    if (chords.length === 0) return m("span", { class: "keybinding-off type-helper text-faint" }, "Off");
    return m(
      "span",
      { class: "flex flex-wrap items-center justify-end gap-x-1.5 gap-y-1" },
      chords.flatMap((chord, index) => {
        const keycap = m(ShortcutKeycap, { key: chord, chord, isApple });
        if (index === 0) return [keycap];
        return [m("span", { key: `or-${chord}`, class: "type-helper text-faint" }, "or"), keycap];
      }),
    );
  }

  function clashView(clash: PendingClash, isApple: boolean, onReplace: () => void, onCancel: () => void): m.Children {
    return m("div", { class: "keybinding-clash mt-3 rounded-md bg-warning-surface p-3" }, [
      ...clash.entries.map((entry) =>
        m("p", { class: "type-helper text-primary" }, clashSentence(clash.cause, entry, isApple)),
      ),
      m(
        "p",
        { class: "type-helper mt-1 text-secondary" },
        clash.entries.length === 1 ? "Replace removes it from there." : "Replace removes them from there.",
      ),
      m("div", { class: "mt-2 flex gap-2" }, [
        m(Button, { sm: true, extra: "keybinding-replace", disabled: isBusy, onclick: onReplace }, "Replace"),
        m(Button, { sm: true, variant: "ghost", extra: "keybinding-clash-cancel", onclick: onCancel }, "Cancel"),
      ]),
    ]);
  }

  /** What Replace will take away from the app's other actions when this edit is saved. */
  function replacementNotes(current: ActionEditor, isApple: boolean): m.Children {
    return Array.from(current.replacedChordsByActionId).flatMap(([otherActionId, remainingChords]) => {
      const other = current.app.actions.find((candidate) => candidate.id === otherActionId);
      if (other === undefined) return [];
      return other.chords
        .filter((chord) => !remainingChords.includes(chord))
        .map((chord) =>
          m(
            "p",
            { class: "keybinding-replaced type-helper mt-2 text-secondary" },
            `${chordSpokenLabel(chord, isApple)} will be removed from "${other.label}".`,
          ),
        );
    });
  }

  function stagedShortcutsView(current: ActionEditor, isApple: boolean): m.Children {
    return m("div", { class: "flex flex-wrap items-center gap-2" }, [
      current.stagedChords.length === 0
        ? m("span", { class: "type-helper text-faint" }, "Off: no shortcut runs this")
        : current.stagedChords.map((chord) =>
            m(
              "span",
              {
                key: chord,
                class:
                  "keybinding-staged inline-flex items-center gap-0.5 rounded-md bg-fill-faint py-0.5 pr-0.5 pl-1",
              },
              [
                m(ShortcutKeycap, { chord, isApple }),
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
              ],
            ),
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
              onclick: () => setRecording(current, true),
            },
            [m.trust(icon("plus", { size: 12 })), " Add shortcut"],
          ),
    ]);
  }

  function recorderView(current: ActionEditor): m.Children {
    if (!current.isRecording) return null;
    return m("div", { class: "mt-3 flex items-center gap-2" }, [
      m("input", {
        class:
          "keybinding-recorder type-body min-w-0 flex-1 rounded-md border border-accent bg-page px-2 py-1 outline-none",
        readonly: true,
        placeholder: "Press the new shortcut",
        "aria-label": "Press the new shortcut",
        oncreate: (created: m.VnodeDOM) => (created.dom as HTMLInputElement).focus(),
        onkeydown: (event: KeyboardEvent) => onRecorderKeydown(current, event),
        onblur: () => setRecording(current, false),
      }),
      m(
        Button,
        {
          sm: true,
          variant: "ghost",
          extra: "keybinding-recorder-cancel",
          onclick: () => setRecording(current, false),
        },
        "Cancel",
      ),
    ]);
  }

  function saveButtons(current: ActionEditor): m.Children {
    const isOwner = catalog?.is_owner === true;
    const canEverywhere = catalog?.can_save_everywhere === true;
    const isSaveBlocked = isBusy || current.isRecording || current.clash !== null || !hasUnsavedChanges(current);
    return m("div", { class: "mt-4 flex flex-wrap items-center gap-2" }, [
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
      m(Button, { sm: true, variant: "ghost", extra: "keybinding-cancel", onclick: closeEditor }, "Cancel"),
      areChordListsEqual(current.stagedChords, current.action.default_chords)
        ? null
        : m(
            Button,
            {
              sm: true,
              variant: "ghost",
              extra: "keybinding-restore-default ml-auto",
              disabled: current.clash !== null,
              onclick: () => restoreDefaults(current),
            },
            "Restore default",
          ),
    ]);
  }

  function editorView(current: ActionEditor, isApple: boolean): m.Children {
    const reservedNotices = current.stagedChords
      .map((chord) => reservedChordNotice(chord, isApple))
      .filter((reserved): reserved is string => reserved !== null);
    const clash = current.clash;
    return m("div", { class: "keybinding-editor mt-1 mb-3 rounded-lg border border-default p-3" }, [
      stagedShortcutsView(current, isApple),
      recorderView(current),
      clash === null
        ? null
        : clashView(
            clash,
            isApple,
            () => replaceClash(current, clash),
            () => {
              current.clash = null;
            },
          ),
      replacementNotes(current, isApple),
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

  function rowMarker(appView: KeybindingAppView, action: KeybindingActionView, isPendingReplace: boolean): m.Children {
    if (isPendingReplace) {
      return m("span", { class: "keybinding-pending type-helper shrink-0 text-secondary" }, "Changes when you save");
    }
    if (!savedActionKeys.has(actionKey(appView.app, action.id))) return null;
    return m("span", { class: "keybinding-saved inline-flex shrink-0 items-center gap-1 type-helper text-success" }, [
      m.trust(icon("check", { size: 12 })),
      "Saved",
    ]);
  }

  function sharedShortcutWarning(appView: KeybindingAppView, action: KeybindingActionView): m.Children {
    const sharing = actionsSharingShortcuts(appView, action.id, (candidate) => shownChords(appView, candidate));
    if (sharing.length === 0) return null;
    const names = sharing.map((other) => `"${other.label}"`).join(", ");
    return m(
      "p",
      { class: "keybinding-shared type-helper mt-0.5 text-warning" },
      `Shares a shortcut with ${names}; edit one of them so each shortcut runs one thing.`,
    );
  }

  function actionRow(appView: KeybindingAppView, action: KeybindingActionView, isApple: boolean): m.Children {
    const isEditing = editor !== null && editor.app.app === appView.app && editor.action.id === action.id;
    const chords = shownChords(appView, action);
    const isPendingReplace = !isEditing && chords !== action.chords;
    const resetClash =
      pendingRevert !== null &&
      pendingRevert.clash.cause === "reset" &&
      pendingRevert.app.app === appView.app &&
      pendingRevert.action.id === action.id
        ? pendingRevert
        : null;
    return m("div", { key: action.id, "data-keybinding-action": `${appView.app}.${action.id}` }, [
      m("div", { class: "flex min-h-10 items-center gap-3 border-b border-default py-2" }, [
        m("div", { class: "min-w-0 flex-1" }, [
          m("span", { class: "type-body text-primary" }, action.label),
          sharedShortcutWarning(appView, action),
        ]),
        rowMarker(appView, action, isPendingReplace),
        m("span", { class: "shrink-0" }, shortcutsView(chords, isApple)),
        m("span", { class: "flex shrink-0 items-center gap-1" }, [
          isResetOffered(action)
            ? m(
                Button,
                {
                  sm: true,
                  variant: "ghost",
                  extra: "keybinding-reset",
                  disabled: isBusy || editor !== null || pendingRevert !== null,
                  ...hoverTooltipAttrs("Undo your change in this workspace"),
                  onclick: () => revert(appView, action, "reset", action.reset_chords),
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
              disabled: isBusy || isEditing || pendingRevert !== null,
              onclick: () => startEditing(appView, action),
            },
            "Edit",
          ),
        ]),
      ]),
      resetClash === null ? null : pendingRevertView(resetClash, isApple),
      isEditing && editor !== null ? editorView(editor, isApple) : null,
    ]);
  }

  function appSection(appView: KeybindingAppView, isApple: boolean): m.Children {
    const isCollapsed = collapsedApps.has(appView.app);
    const count = appView.actions.length;
    return m("section", { key: appView.app, class: "mb-5", "data-keybinding-app": appView.app }, [
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

  function discard(loaded: KeybindingsCatalog, entry: UnsentKeybindingView): void {
    const appView = loaded.apps.find((candidate) => candidate.app === entry.app);
    const action = appView?.actions.find((candidate) => candidate.id === entry.action);
    if (appView === undefined || action === undefined) {
      void act(() => discardKeybinding(entry.app, entry.action));
      return;
    }
    revert(appView, action, "discard", entry.restored_chords);
  }

  function unsentBanner(loaded: KeybindingsCatalog, isApple: boolean): m.Children {
    if (loaded.unsent.length === 0) return null;
    return m("div", { class: "mb-4 rounded-md bg-warning-surface p-3", "data-keybinding-unsent": true }, [
      m(
        "p",
        { class: "type-helper text-primary" },
        loaded.has_pending_permission
          ? "Waiting for your approval in Imbue Studio; these apply in this workspace meanwhile."
          : "Not yet saved to your account; these apply in this workspace meanwhile.",
      ),
      ...loaded.unsent.map((entry) => {
        const discardClash =
          pendingRevert !== null &&
          pendingRevert.clash.cause === "discard" &&
          pendingRevert.app.app === entry.app &&
          pendingRevert.action.id === entry.action
            ? pendingRevert
            : null;
        return m("div", { "data-keybinding-unsent-entry": `${entry.app}.${entry.action}` }, [
          m("div", { class: "mt-1 flex items-center justify-between gap-2" }, [
            m("span", { class: "type-helper min-w-0 truncate text-secondary" }, unsentEntryLabel(loaded, entry)),
            m(
              Button,
              {
                sm: true,
                variant: "ghost",
                extra: "keybinding-discard",
                disabled: isBusy || pendingRevert !== null,
                onclick: () => discard(loaded, entry),
              },
              "Discard",
            ),
          ]),
          discardClash === null ? null : pendingRevertView(discardClash, isApple),
        ]);
      }),
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
    onremove() {
      closeEditor();
    },
    view(vnode) {
      onRecordingChange = vnode.attrs.onRecordingChange;
      if (loadError !== null) {
        return m("p", { class: "type-helper text-danger" }, `The shortcuts could not be loaded: ${loadError}`);
      }
      if (catalog === null) return m("p", { class: "type-helper text-secondary" }, "Loading the shortcuts...");
      const isApple = vnode.attrs.isApple ?? isApplePlatform();
      const loaded = catalog;
      return m("div", { "data-keyboard-settings": true }, [
        unsentBanner(loaded, isApple),
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
