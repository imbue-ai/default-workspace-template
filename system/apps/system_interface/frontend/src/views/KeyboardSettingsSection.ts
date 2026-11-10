/**
 * The Keyboard settings section (user-profile-state spec, section 10.3): every app's actions from their
 * manifests, grouped by app, each with its shortcuts drawn as keycaps. It is the body of the `/keyboard-shortcuts`
 * page that Imbue Studio's Workspace settings frames, and of the Keyboard shortcuts dialog a plain browser opens
 * from the backdrop's context menu.
 *
 * Shortcuts are read in a context (the keyboard-shortcuts spec in the mngr repo, sections 4 and 7): the platform,
 * which decides what the keys are called and which chords cannot arrive here (a Mac's Control chord off a Mac), and
 * the surface, which hides the chords a browser keeps from its pages when the workspace runs in one. A row whose
 * shortcuts are all hidden here reads "Off", with the hidden ones named on hover. A chord a browser would keep, one
 * the OS is known to keep, or one the desktop app takes from the OS is saved with a warning; only Ctrl+Alt+Del is
 * refused.
 *
 * Editing one action opens its editor below the row: the shortcuts it will have, each removable, a recorder that
 * adds one more, and the save surfaces. The recorder takes every key, Escape included (it has a Cancel of its own),
 * and says when it is listening, so whatever frames it can leave those keys alone.
 *
 * No step leaves one shortcut on two actions of an app without asking: recording a shortcut another action uses,
 * restoring the defaults, and resetting a change each offer Replace (take it from the other action, saved together)
 * or Cancel when what they would bring in is in use. A replaced shortcut leaves the other action's row at once,
 * marked as changing on save; a shortcut that still ends up on two actions (from layers this screen does not edit)
 * is flagged on both rows.
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
import {
  type ChordContext,
  chordDisplayLabel,
  chordFromKeydown,
  chordNotices,
  chordPlatformName,
  chordSpokenLabel,
  detectChordPlatform,
  isChordHiddenIn,
  isRefusedChord,
} from "@imbue/workspace-ui/src/keybindings";
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

/** The chords of the list that can arrive in the context; every chord when no context is given. */
function visibleIn(chords: readonly string[], context: ChordContext | null): readonly string[] {
  return context === null ? chords : chords.filter((chord) => !isChordHiddenIn(chord, context));
}

/** Every other action of the app using one of `chords` now, once per chord it uses (conflicts are per app). An
 *  action's chords are read from `chordsByActionId` ahead of what it was loaded with, for edits not yet saved.
 *  Only chords visible in `context` clash: a chord that cannot arrive here runs nothing here. */
export function clashesOf(
  app: KeybindingAppView,
  actionId: string,
  chords: readonly string[],
  chordsByActionId: ReadonlyMap<string, readonly string[]> = new Map(),
  context: ChordContext | null = null,
): ClashEntry[] {
  return visibleIn(chords, context).flatMap((chord) =>
    app.actions
      .filter(
        (action) =>
          action.id !== actionId &&
          visibleIn(chordsByActionId.get(action.id) ?? action.chords, context).includes(chord),
      )
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

/** The one chord the recorder refuses, and why. */
export const REFUSED_CHORD_NOTICE = "Ctrl+Alt+Delete belongs to Windows and can't be a shortcut.";

/** What a row says on hover when its shortcuts are set but none can arrive here. */
export function hiddenChordsHint(hidden: readonly string[], context: ChordContext): string {
  const isApple = context.platform === "mac";
  const labels = hidden.map((chord) => chordDisplayLabel(chord, isApple)).join(", ");
  const verb = hidden.length === 1 ? "is" : "are";
  const where = context.surface === "browser" ? "in a browser" : `on ${chordPlatformName(context.platform)}`;
  return `${labels} ${verb} set, but not available ${where}.`;
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
  /** Why the last recorded key was refused, until the next one is recorded. */
  refusal: string | null;
}

/** A reset or a discard whose restored shortcuts other actions use, waiting on Replace or Cancel. */
interface PendingRevert {
  readonly app: KeybindingAppView;
  readonly action: KeybindingActionView;
  readonly clash: PendingClash;
}

export interface KeyboardSettingsSectionAttrs {
  /** Where the shortcuts are read: the browser's platform in the desktop app when omitted. */
  readonly context?: ChordContext;
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
  let context: ChordContext = { platform: detectChordPlatform(), surface: "desktop" };
  const collapsedApps = new Set<string>();
  // The actions saved or reset since this section opened, marked until it is opened again.
  const savedActionKeys = new Set<string>();

  const isApple = (): boolean => context.platform === "mac";

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
      refusal: null,
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
    const chord = chordFromKeydown(event, context.platform);
    if (chord === null) return;
    current.refusal = null;
    if (isRefusedChord(chord)) {
      current.refusal = REFUSED_CHORD_NOTICE;
      setRecording(current, false);
      return;
    }
    const entries = clashesOf(current.app, current.action.id, [chord], current.replacedChordsByActionId, context);
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
      context,
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

  /** The chords an action has: what is saved, or what Replace has left another action of the app being edited. */
  function heldChords(appView: KeybindingAppView, action: KeybindingActionView): readonly string[] {
    if (editor === null || editor.app.app !== appView.app) return action.chords;
    return editor.replacedChordsByActionId.get(action.id) ?? action.chords;
  }

  /** The chords a row shows: the held ones that can arrive here. */
  function shownChords(appView: KeybindingAppView, action: KeybindingActionView): readonly string[] {
    return visibleIn(heldChords(appView, action), context);
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
    const entries = clashesOf(appView, action.id, restoredChords, new Map(), context);
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
  function pendingRevertView(current: PendingRevert): m.Children {
    return m(
      "div",
      { class: "mb-3" },
      clashView(
        current.clash,
        () => replaceAndRevert(current),
        () => {
          pendingRevert = null;
        },
      ),
    );
  }

  /** Each visible shortcut its own keycap, with "or" between the alternatives; "Off" when none can arrive here,
   *  naming on hover the ones that are set all the same. */
  function shortcutsView(chords: readonly string[], hidden: readonly string[]): m.Children {
    if (chords.length === 0) {
      const hint = hidden.length === 0 ? {} : hoverTooltipAttrs(hiddenChordsHint(hidden, context));
      return m(
        "span",
        { class: "keybinding-off type-helper text-faint", "data-hidden-chords": hidden.join(" "), ...hint },
        "Off",
      );
    }
    return m(
      "span",
      { class: "flex flex-wrap items-center justify-end gap-x-1.5 gap-y-1" },
      chords.flatMap((chord, index) => {
        const keycap = m(ShortcutKeycap, { key: chord, chord, isApple: isApple() });
        if (index === 0) return [keycap];
        return [m("span", { key: `or-${chord}`, class: "type-helper text-faint" }, "or"), keycap];
      }),
    );
  }

  function clashView(clash: PendingClash, onReplace: () => void, onCancel: () => void): m.Children {
    return m("div", { class: "keybinding-clash mt-3 rounded-md bg-warning-surface p-3" }, [
      ...clash.entries.map((entry) =>
        m("p", { class: "type-helper text-primary" }, clashSentence(clash.cause, entry, isApple())),
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
  function replacementNotes(current: ActionEditor): m.Children {
    return Array.from(current.replacedChordsByActionId).flatMap(([otherActionId, remainingChords]) => {
      const other = current.app.actions.find((candidate) => candidate.id === otherActionId);
      if (other === undefined) return [];
      return other.chords
        .filter((chord) => !remainingChords.includes(chord))
        .map((chord) =>
          m(
            "p",
            { class: "keybinding-replaced type-helper mt-2 text-secondary" },
            `${chordSpokenLabel(chord, isApple())} will be removed from "${other.label}".`,
          ),
        );
    });
  }

  function stagedShortcutsView(current: ActionEditor): m.Children {
    return m("div", { class: "flex flex-wrap items-center gap-2" }, [
      current.stagedChords.length === 0
        ? m("span", { class: "type-helper text-faint" }, "Off: no shortcut runs this")
        : current.stagedChords.map((chord) => {
            const isHidden = isChordHiddenIn(chord, context);
            return m(
              "span",
              {
                key: chord,
                class:
                  "keybinding-staged inline-flex items-center gap-0.5 rounded-md bg-fill-faint py-0.5 pr-0.5 pl-1" +
                  (isHidden ? " opacity-60" : ""),
                "data-hidden": isHidden ? "true" : undefined,
                ...(isHidden ? hoverTooltipAttrs(hiddenChordsHint([chord], context)) : {}),
              },
              [
                m(ShortcutKeycap, { chord, isApple: isApple() }),
                m(
                  Button,
                  {
                    xs: true,
                    icon: true,
                    variant: "ghost",
                    extra: "keybinding-remove",
                    "aria-label": `Remove ${chordSpokenLabel(chord, isApple())}`,
                    ...hoverTooltipAttrs(`Remove ${chordSpokenLabel(chord, isApple())}`),
                    onclick: () => {
                      current.stagedChords = current.stagedChords.filter((staged) => staged !== chord);
                    },
                  },
                  m.trust(icon("close", { size: 12 })),
                ),
              ],
            );
          }),
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

  function editorView(current: ActionEditor): m.Children {
    const warnings = current.stagedChords.flatMap((chord) => chordNotices(chord, context));
    const clash = current.clash;
    return m("div", { class: "keybinding-editor mt-1 mb-3 rounded-lg border border-default p-3" }, [
      stagedShortcutsView(current),
      recorderView(current),
      current.refusal === null
        ? null
        : m("p", { class: "keybinding-refused type-helper mt-2 text-danger" }, current.refusal),
      clash === null
        ? null
        : clashView(
            clash,
            () => replaceClash(current, clash),
            () => {
              current.clash = null;
            },
          ),
      replacementNotes(current),
      ...warnings.map((warning) => m("p", { class: "keybinding-warning type-helper mt-2 text-warning" }, warning)),
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

  function actionRow(appView: KeybindingAppView, action: KeybindingActionView): m.Children {
    const isEditing = editor !== null && editor.app.app === appView.app && editor.action.id === action.id;
    const held = heldChords(appView, action);
    const chords = visibleIn(held, context);
    const hidden = held.filter((chord) => !chords.includes(chord));
    const isPendingReplace = !isEditing && held !== action.chords;
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
        m("span", { class: "shrink-0" }, shortcutsView(chords, hidden)),
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
      resetClash === null ? null : pendingRevertView(resetClash),
      isEditing && editor !== null ? editorView(editor) : null,
    ]);
  }

  function appSection(appView: KeybindingAppView): m.Children {
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
            appView.actions.map((action) => actionRow(appView, action)),
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
          discardClash === null ? null : pendingRevertView(discardClash),
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
      if (vnode.attrs.context !== undefined) context = vnode.attrs.context;
      if (loadError !== null) {
        return m("p", { class: "type-helper text-danger" }, `The shortcuts could not be loaded: ${loadError}`);
      }
      if (catalog === null) return m("p", { class: "type-helper text-secondary" }, "Loading the shortcuts...");
      const loaded = catalog;
      return m("div", { "data-keyboard-settings": true }, [
        unsentBanner(loaded),
        notice !== null ? m("p", { class: "keybinding-notice type-helper mb-3 text-secondary" }, notice) : null,
        m(
          "div",
          { class: "keybinding-apps" },
          loaded.apps.filter((appView) => appView.actions.length > 0).map((appView) => appSection(appView)),
        ),
      ]);
    },
  };
}
