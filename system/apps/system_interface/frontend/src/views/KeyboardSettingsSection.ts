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
 *
 * In the desktop app the section ends with Imbue Studio's own shortcuts (the keyboard-shortcuts spec in the mngr repo,
 * section 8), which the chrome hands the framed page: they live in the account, so a row has one Save, which asks the
 * chrome to store it and shows as saving until the chrome sends them again. The desktop app runs them before any page
 * sees the key, so they clash with every action and need a real modifier, as workspace-wide actions do, and no
 * browser ever runs them, so nothing warns that a browser keeps one. Replace between the two kinds saves each where
 * it lives: an action of the workspace's that an Imbue Studio shortcut takes a chord from is saved "everywhere", and
 * an Imbue Studio action is always saved through the chrome.
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
  hiddenChordReason,
  isChordHiddenIn,
  isRefusedChord,
  isWorkspaceWideChord,
  readsChordAsApple,
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
import type { ChromeShortcuts } from "../model/chromeShortcuts";
import { setShortcutRecording } from "../model/shortcutRecording";
import { ShortcutKeycap } from "./ShortcutKeycap";

/** One shortcut a step would bring to an action that another action uses now. */
export interface ClashEntry {
  readonly chord: string;
  readonly otherApp: KeybindingAppView;
  readonly otherAction: KeybindingActionView;
}

/** An action with the app it belongs to. */
interface CatalogAction {
  readonly app: KeybindingAppView;
  readonly action: KeybindingActionView;
}

function actionKey(app: string, actionId: string): string {
  return `${app}.${actionId}`;
}

/** The chords of the list that can arrive in the context; every chord when no context is given. */
function visibleIn(chords: readonly string[], context: ChordContext | null): readonly string[] {
  return context === null ? chords : chords.filter((chord) => !isChordHiddenIn(chord, context));
}

/** The actions a chord of this action could collide with (the keyboard-shortcuts spec in the mngr repo, section
 *  5.3): the other actions of its own app; every action of every app for a workspace-wide action, since it wins
 *  inside any app's page; and, for an app's own action, the workspace-wide actions of every other app. */
export function clashCandidates(
  loaded: KeybindingsCatalog,
  app: KeybindingAppView,
  actionId: string,
): CatalogAction[] {
  const own = app.actions.find((action) => action.id === actionId);
  const isWorkspaceWide = own?.scope === "workspace";
  return loaded.apps.flatMap((other) =>
    other.actions
      .filter((action) => !(other.app === app.app && action.id === actionId))
      .filter((action) => other.app === app.app || isWorkspaceWide || action.scope === "workspace")
      .map((action) => ({ app: other, action })),
  );
}

/** Every candidate using one of `chords` now, once per chord it uses; `heldChordsOf` answers what each candidate
 *  has, edits not yet saved included. Only chords visible in `context` clash: a chord that cannot arrive here runs
 *  nothing here. */
function clashesAmong(
  candidates: readonly CatalogAction[],
  chords: readonly string[],
  heldChordsOf: (candidate: CatalogAction) => readonly string[],
  context: ChordContext | null,
): ClashEntry[] {
  return visibleIn(chords, context).flatMap((chord) =>
    candidates
      .filter((candidate) => visibleIn(heldChordsOf(candidate), context).includes(chord))
      .map((candidate) => ({ chord, otherApp: candidate.app, otherAction: candidate.action })),
  );
}

/** Every other action of the app using one of `chords` now, once per chord it uses. An action's chords are read
 *  from `chordsByActionId` ahead of what it was loaded with, for edits not yet saved. */
export function clashesOf(
  app: KeybindingAppView,
  actionId: string,
  chords: readonly string[],
  chordsByActionId: ReadonlyMap<string, readonly string[]> = new Map(),
  context: ChordContext | null = null,
): ClashEntry[] {
  const candidates = app.actions.filter((action) => action.id !== actionId).map((action) => ({ app, action }));
  return clashesAmong(
    candidates,
    chords,
    (candidate) => chordsByActionId.get(candidate.action.id) ?? candidate.action.chords,
    context,
  );
}

/** The clashes across the whole catalog, by the rule of `clashCandidates`; `replacedChordsByKey` holds what Replace
 *  has left other actions (by `actionKey`), ahead of what they were loaded with. */
export function clashesInCatalog(
  loaded: KeybindingsCatalog,
  app: KeybindingAppView,
  actionId: string,
  chords: readonly string[],
  replacedChordsByKey: ReadonlyMap<string, readonly string[]>,
  context: ChordContext | null,
): ClashEntry[] {
  return clashesAmong(
    clashCandidates(loaded, app, actionId),
    chords,
    (candidate) =>
      replacedChordsByKey.get(actionKey(candidate.app.app, candidate.action.id)) ?? candidate.action.chords,
    context,
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

/** The app name Imbue Studio's own shortcuts are listed and stored under. */
export const IMBUE_STUDIO_APP = "imbue-studio";

function isImbueStudio(appView: KeybindingAppView): boolean {
  return appView.app === IMBUE_STUDIO_APP;
}

/** Imbue Studio's shortcuts as the section lists an app's. They are `workspace`-scoped because the desktop app runs
 *  them wherever focus is, ahead of every page, which gives them the clash and modifier rules of such actions. */
export function imbueStudioAppView(shortcuts: ChromeShortcuts): KeybindingAppView {
  return {
    app: IMBUE_STUDIO_APP,
    display_name: "Imbue Studio",
    actions: shortcuts.actions.map((action) => ({
      id: action.id,
      label: action.label,
      default_chords: [...action.defaultChords],
      chords: [...action.chords],
      reset_chords: [...action.defaultChords],
      source: areChordListsEqual(action.chords, action.defaultChords) ? "default" : "account",
      scope: "workspace",
    })),
  };
}

/** Whether restoring an Imbue Studio action's defaults would change what it runs here. */
export function differsFromDefaults(action: KeybindingActionView, context: ChordContext | null): boolean {
  const current = visibleIn(action.chords, context);
  const defaults = new Set(visibleIn(action.default_chords, context));
  return current.length !== defaults.size || current.some((chord) => !defaults.has(chord));
}

/** The one chord the recorder refuses, and why. */
export const REFUSED_CHORD_NOTICE = "Ctrl+Alt+Delete belongs to Windows and can't be a shortcut.";

/** Why the recorder refuses a chord for a workspace-wide action. */
export const WORKSPACE_WIDE_CHORD_NOTICE =
  "A shortcut that works everywhere in the workspace needs a modifier other than Shift, or a function key.";

/** What a row says on hover when its shortcuts are set but none can arrive here: each hidden chord with why, the
 *  platform's (a Mac's Control chord, named with the Mac's symbols) or the browser's. */
export function hiddenChordsHint(hidden: readonly string[], context: ChordContext): string {
  const sentence = (chords: readonly string[], where: string): string => {
    const labels = chords
      .map((chord) => chordDisplayLabel(chord, readsChordAsApple(chord, context.platform)))
      .join(", ");
    return `${labels} ${chords.length === 1 ? "is" : "are"} set, but not available ${where}.`;
  };
  const onPlatform = hidden.filter((chord) => hiddenChordReason(chord, context) === "platform");
  const inBrowser = hidden.filter((chord) => !onPlatform.includes(chord));
  const sentences: string[] = [];
  if (onPlatform.length > 0) sentences.push(sentence(onPlatform, `on ${chordPlatformName(context.platform)}`));
  if (inBrowser.length > 0) sentences.push(sentence(inBrowser, "in a browser"));
  return sentences.join(" ");
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

/** Whether a reset would change what the action runs here: it has a change in this workspace, and the chords of that
 *  change that can arrive in the context differ from those of what removing it restores (a Replace elsewhere can
 *  leave a change equal to it). */
export function isResetOffered(action: KeybindingActionView, context: ChordContext | null): boolean {
  if (action.source !== "workspace") return false;
  const current = visibleIn(action.chords, context);
  const restored = new Set(visibleIn(action.reset_chords, context));
  return current.length !== restored.size || current.some((chord) => !restored.has(chord));
}

/** What brought shortcuts in use elsewhere into an action: a recorded one, its defaults, a reset of this
 *  workspace's change, or a discard of an undelivered "everywhere" save. */
type ClashCause = "record" | "restore-default" | "reset" | "discard";

/** Shortcuts a step would bring in that other actions use, until the person replaces or cancels. */
interface PendingClash {
  readonly cause: ClashCause;
  readonly entries: readonly ClashEntry[];
}

/** What Replace left another action: its app, the action, and the chords it keeps. */
interface ReplacedAction {
  readonly app: KeybindingAppView;
  readonly action: KeybindingActionView;
  readonly chords: readonly string[];
}

/** The one action being edited: the chords it will have, and what Replace left other actions, by `actionKey`. */
interface ActionEditor {
  readonly app: KeybindingAppView;
  readonly action: KeybindingActionView;
  stagedChords: string[];
  readonly replacedByKey: Map<string, ReplacedAction>;
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
  /** Handed, while the section is mounted, what records a key the desktop app took while the recorder listened
   *  (`minds:record-chord`), as if it were pressed; handed null as the section goes. */
  readonly onChordRelay?: (record: ((chord: string) => void) | null) => void;
  /** Imbue Studio's own shortcuts, as the desktop app sent them last; listed last, and only in the desktop app. */
  readonly chromeShortcuts?: ChromeShortcuts | null;
  /** Asks the desktop app to save one of Imbue Studio's shortcuts; it answers with `chromeShortcuts` again. */
  readonly onSaveChromeShortcut?: (id: string, chords: readonly string[]) => void;
}

const SECTION_HEADER_CLASS =
  "keybinding-app-header flex w-full items-center gap-2 rounded-md px-1 py-1.5 text-left hover:bg-fill-hover";

/** The other action as a sentence names it: its label, and its app when that is another app. */
function otherActionName(entry: ClashEntry, ownApp: string): string {
  const label = `"${entry.otherAction.label}"`;
  return entry.otherApp.app === ownApp ? label : `${label} (${entry.otherApp.display_name})`;
}

/** The sentence a clash box opens with: which shortcut, what has it now, and what the step would do with it. */
function clashSentence(cause: ClashCause, entry: ClashEntry, ownApp: string, isApple: boolean): string {
  const shortcut = chordSpokenLabel(entry.chord, isApple);
  const other = otherActionName(entry, ownApp);
  if (cause === "record") return `${shortcut} already runs ${other}.`;
  if (cause === "restore-default") return `The default includes ${shortcut}, which ${other} uses now.`;
  const step = cause === "reset" ? "Resetting" : "Discarding";
  return `${step} brings back ${shortcut}, which ${other} uses now.`;
}

/** The chords each clashing action keeps once the clashing ones are taken off it, by `actionKey`. */
function remainingAfterReplace(
  entries: readonly ClashEntry[],
  replacedByKey: ReadonlyMap<string, ReplacedAction>,
): Map<string, ReplacedAction> {
  const remaining = new Map(replacedByKey);
  for (const entry of entries) {
    const key = actionKey(entry.otherApp.app, entry.otherAction.id);
    const held = remaining.get(key)?.chords ?? entry.otherAction.chords;
    remaining.set(key, {
      app: entry.otherApp,
      action: entry.otherAction,
      chords: held.filter((chord) => chord !== entry.chord),
    });
  }
  return remaining;
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
  let chromeShortcuts: ChromeShortcuts | null = null;
  let onSaveChromeShortcut: ((id: string, chords: readonly string[]) => void) | undefined;
  const collapsedApps = new Set<string>();
  // The actions saved or reset since this section opened, marked until it is opened again.
  const savedActionKeys = new Set<string>();
  // The Imbue Studio actions asked to be saved, oldest first: the chrome answers each save with its shortcuts, in
  // the order the saves were sent.
  const pendingChromeActionIds: string[] = [];
  // Why the chrome could not save an Imbue Studio action, until it is saved again.
  const chromeErrorByActionId = new Map<string, string>();

  const isApple = (): boolean => context.platform === "mac";
  // A chord reads with the Mac's symbols on a Mac, and elsewhere when it holds the Mac's Control key.
  const isAppleFor = (chord: string): boolean => readsChordAsApple(chord, context.platform);

  /** The catalog the section lists: the workspace's apps, then Imbue Studio's shortcuts in the desktop app. */
  function shownCatalog(): KeybindingsCatalog | null {
    if (catalog === null || chromeShortcuts === null || context.surface !== "desktop") return catalog;
    return { ...catalog, apps: [...catalog.apps, imbueStudioAppView(chromeShortcuts)] };
  }

  /** Take the shortcuts the chrome sent: the oldest save asked for is done, or failed with its error. */
  function receiveChromeShortcuts(next: ChromeShortcuts | null): void {
    chromeShortcuts = next;
    if (next === null) return;
    const actionId = pendingChromeActionIds.shift();
    if (actionId === undefined) {
      if (next.error !== null) notice = next.error;
      return;
    }
    if (next.error === null) {
      chromeErrorByActionId.delete(actionId);
      savedActionKeys.add(actionKey(IMBUE_STUDIO_APP, actionId));
    } else {
      chromeErrorByActionId.set(actionId, next.error);
    }
  }

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
      replacedByKey: new Map(),
      isRecording: false,
      clash: null,
      refusal: null,
    };
    notice = null;
  }

  function hasUnsavedChanges(current: ActionEditor): boolean {
    return !areChordListsEqual(current.stagedChords, current.action.chords) || current.replacedByKey.size > 0;
  }

  function addChord(current: ActionEditor, chord: string): void {
    if (!current.stagedChords.includes(chord)) current.stagedChords = [...current.stagedChords, chord];
  }

  /** What Replace has left each action so far, as the clash check reads it. */
  function replacedChordsByKey(current: ActionEditor): ReadonlyMap<string, readonly string[]> {
    return new Map(Array.from(current.replacedByKey, ([key, replaced]) => [key, replaced.chords]));
  }

  function clashesWith(current: ActionEditor, chords: readonly string[]): ClashEntry[] {
    const shown = shownCatalog();
    if (shown === null) return [];
    return clashesInCatalog(shown, current.app, current.action.id, chords, replacedChordsByKey(current), context);
  }

  function onRecorderKeydown(current: ActionEditor, event: KeyboardEvent): void {
    event.preventDefault();
    event.stopPropagation();
    const chord = chordFromKeydown(event, context.platform);
    if (chord !== null) recordChord(current, chord);
  }

  /** A key the desktop app took while the recorder listened, recorded as a pressed one is. */
  function recordRelayedChord(chord: string): void {
    if (editor === null || !editor.isRecording) return;
    recordChord(editor, chord);
    m.redraw();
  }

  /** Take one chord into the editor: refused, offered as a clash, or staged; the recorder stops either way. */
  function recordChord(current: ActionEditor, chord: string): void {
    current.refusal = null;
    if (isRefusedChord(chord)) current.refusal = REFUSED_CHORD_NOTICE;
    else if (current.action.scope === "workspace" && !isWorkspaceWideChord(chord)) {
      current.refusal = WORKSPACE_WIDE_CHORD_NOTICE;
    }
    if (current.refusal !== null) {
      setRecording(current, false);
      return;
    }
    const entries = clashesWith(current, [chord]);
    if (entries.length === 0) addChord(current, chord);
    else current.clash = { cause: "record", entries };
    setRecording(current, false);
  }

  function restoreDefaults(current: ActionEditor): void {
    const entries = clashesWith(current, current.action.default_chords);
    if (entries.length === 0) current.stagedChords = [...current.action.default_chords];
    else current.clash = { cause: "restore-default", entries };
  }

  /** Take every clashing shortcut off the action that has it now (saved with the edit), then finish the step. */
  function replaceClash(current: ActionEditor, clash: PendingClash): void {
    for (const [key, replaced] of remainingAfterReplace(clash.entries, current.replacedByKey)) {
      current.replacedByKey.set(key, replaced);
    }
    if (clash.cause === "restore-default") current.stagedChords = [...current.action.default_chords];
    else for (const entry of clash.entries) addChord(current, entry.chord);
    current.clash = null;
  }

  /** The chords an action has: what is saved, or what Replace has left it while another action is being edited. */
  function heldChords(appView: KeybindingAppView, action: KeybindingActionView): readonly string[] {
    if (editor === null) return action.chords;
    return editor.replacedByKey.get(actionKey(appView.app, action.id))?.chords ?? action.chords;
  }

  /** The chords a row shows: the held ones that can arrive here. */
  function shownChords(appView: KeybindingAppView, action: KeybindingActionView): readonly string[] {
    return visibleIn(heldChords(appView, action), context);
  }

  /** What a delivery attempt leaves on screen: nothing once it landed, else its own word on why not. */
  function noticeOfDelivery(delivery: KeybindingsDeliveryOutcome | null): string | null {
    return delivery === null || delivery.status === "DELIVERED" ? null : delivery.detail;
  }

  /** The save an action of the workspace's takes when an Imbue Studio shortcut takes a chord from it: the account's,
   *  where Imbue Studio's live, unless this workspace cannot save there. */
  function accountScopeIfAvailable(): "workspace" | "account" {
    return catalog?.can_save_everywhere === true ? "account" : "workspace";
  }

  /** Save one action where it lives: an Imbue Studio action through the chrome, marked saved once the chrome sends
   *  its shortcuts again; any other through the shell, in `scope`. Answers the shell's delivery, if any. */
  async function saveAction(
    app: string,
    actionId: string,
    chords: readonly string[],
    scope: "workspace" | "account",
  ): Promise<KeybindingsDeliveryOutcome | null> {
    if (app === IMBUE_STUDIO_APP) {
      chromeErrorByActionId.delete(actionId);
      pendingChromeActionIds.push(actionId);
      onSaveChromeShortcut?.(actionId, [...chords]);
      return null;
    }
    const { delivery } = await saveKeybinding(app, actionId, [...chords], scope);
    savedActionKeys.add(actionKey(app, actionId));
    return delivery;
  }

  async function save(current: ActionEditor, scope: "workspace" | "account"): Promise<void> {
    isBusy = true;
    const replacedScope = isImbueStudio(current.app) ? accountScopeIfAvailable() : scope;
    try {
      let lastDelivery: KeybindingsDeliveryOutcome | null = null;
      for (const replaced of current.replacedByKey.values()) {
        lastDelivery =
          (await saveAction(replaced.app.app, replaced.action.id, replaced.chords, replacedScope)) ?? lastDelivery;
      }
      lastDelivery =
        (await saveAction(current.app.app, current.action.id, current.stagedChords, scope)) ?? lastDelivery;
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
    const shown = shownCatalog();
    const entries =
      shown === null ? [] : clashesInCatalog(shown, appView, action.id, restoredChords, new Map(), context);
    if (entries.length > 0) {
      pendingRevert = { app: appView, action, clash: { cause, entries } };
      return;
    }
    void act(() => runRevert(appView, action, cause));
  }

  /** Undo the change: an Imbue Studio action goes back to its defaults through the chrome; any other drops this
   *  workspace's change or the undelivered save. */
  async function runRevert(
    appView: KeybindingAppView,
    action: KeybindingActionView,
    cause: ClashCause,
  ): Promise<void> {
    if (isImbueStudio(appView)) {
      await saveAction(appView.app, action.id, action.default_chords, "account");
      return;
    }
    if (cause === "discard") await discardKeybinding(appView.app, action.id);
    else await resetKeybinding(appView.app, action.id);
    savedActionKeys.add(actionKey(appView.app, action.id));
  }

  /** Take the restored shortcuts off the actions that have them now, each where it lives (an action of the
   *  workspace's in this workspace, or for an Imbue Studio reset "everywhere"), then revert. */
  function replaceAndRevert(current: PendingRevert): void {
    pendingRevert = null;
    const replacedScope = isImbueStudio(current.app) ? accountScopeIfAvailable() : "workspace";
    void act(async () => {
      for (const replaced of remainingAfterReplace(current.clash.entries, new Map()).values()) {
        await saveAction(replaced.app.app, replaced.action.id, replaced.chords, replacedScope);
      }
      await runRevert(current.app, current.action, current.clash.cause);
    });
  }

  /** A pending revert's Replace / Cancel box, where it was asked for. */
  function pendingRevertView(current: PendingRevert): m.Children {
    return m(
      "div",
      { class: "mb-3" },
      clashView(
        current.clash,
        current.app.app,
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

  function clashView(clash: PendingClash, ownApp: string, onReplace: () => void, onCancel: () => void): m.Children {
    return m("div", { class: "keybinding-clash mt-3 rounded-md bg-warning-surface p-3" }, [
      ...clash.entries.map((entry) =>
        m("p", { class: "type-helper text-primary" }, clashSentence(clash.cause, entry, ownApp, isApple())),
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

  /** What Replace will take away from other actions when this edit is saved. */
  function replacementNotes(current: ActionEditor): m.Children {
    return Array.from(current.replacedByKey.values()).flatMap((replaced) => {
      const name =
        replaced.app.app === current.app.app
          ? `"${replaced.action.label}"`
          : `"${replaced.action.label}" (${replaced.app.display_name})`;
      return replaced.action.chords
        .filter((chord) => !replaced.chords.includes(chord))
        .map((chord) =>
          m(
            "p",
            { class: "keybinding-replaced type-helper mt-2 text-secondary" },
            `${chordSpokenLabel(chord, isAppleFor(chord))} will be removed from ${name}.`,
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
                m(ShortcutKeycap, { chord, isApple: isAppleFor(chord) }),
                m(
                  Button,
                  {
                    xs: true,
                    icon: true,
                    variant: "ghost",
                    extra: "keybinding-remove",
                    "aria-label": `Remove ${chordSpokenLabel(chord, isAppleFor(chord))}`,
                    ...hoverTooltipAttrs(`Remove ${chordSpokenLabel(chord, isAppleFor(chord))}`),
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

  /** The warnings of a chord on one of the app's rows; no browser runs an Imbue Studio shortcut, so a browser keeping
   *  one is no warning there. */
  function noticesOf(appView: KeybindingAppView, chord: string): string[] {
    return chordNotices(chord, context, { isBrowserReservedNoticed: !isImbueStudio(appView) });
  }

  /** An Imbue Studio row's one save: to the account, through the chrome. */
  function imbueStudioSaveButtons(current: ActionEditor, isSaveBlocked: boolean): m.Children {
    return m(
      Button,
      {
        sm: true,
        variant: "primary",
        disabled: isSaveBlocked,
        ...hoverTooltipAttrs("Save for your account, in every Imbue Studio window"),
        onclick: () => void save(current, "account"),
      },
      "Save",
    );
  }

  function saveButtons(current: ActionEditor): m.Children {
    const isOwner = catalog?.is_owner === true;
    const canEverywhere = catalog?.can_save_everywhere === true;
    const isSaveBlocked = isBusy || current.isRecording || current.clash !== null || !hasUnsavedChanges(current);
    const isChrome = isImbueStudio(current.app);
    return m("div", { class: "mt-4 flex flex-wrap items-center gap-2" }, [
      isChrome
        ? imbueStudioSaveButtons(current, isSaveBlocked)
        : m(
            Button,
            { sm: true, variant: "primary", disabled: isSaveBlocked, onclick: () => void save(current, "workspace") },
            isOwner ? "Only in this workspace" : "Save",
          ),
      isOwner && !isChrome
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
    const warnings = current.stagedChords.flatMap((chord) => noticesOf(current.app, chord));
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
            current.app.app,
            () => replaceClash(current, clash),
            () => {
              current.clash = null;
            },
          ),
      replacementNotes(current),
      ...warnings.map((warning) => m("p", { class: "keybinding-warning type-helper mt-2 text-warning" }, warning)),
      !isImbueStudio(current.app) && catalog?.is_owner === true && catalog.can_save_everywhere !== true
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
    if (isImbueStudio(appView) && pendingChromeActionIds.includes(action.id)) {
      return m("span", { class: "keybinding-saving type-helper shrink-0 text-secondary" }, "Saving...");
    }
    if (!savedActionKeys.has(actionKey(appView.app, action.id))) return null;
    return m("span", { class: "keybinding-saved inline-flex shrink-0 items-center gap-1 type-helper text-success" }, [
      m.trust(icon("check", { size: 12 })),
      "Saved",
    ]);
  }

  /** The actions, of this app or workspace-wide ones elsewhere, sharing one of this action's shown shortcuts. */
  function sharedShortcutWarning(appView: KeybindingAppView, action: KeybindingActionView): m.Children {
    const shown = shownCatalog();
    if (shown === null) return null;
    const own = shownChords(appView, action);
    const sharing = clashCandidates(shown, appView, action.id).filter((candidate) =>
      shownChords(candidate.app, candidate.action).some((chord) => own.includes(chord)),
    );
    if (sharing.length === 0) return null;
    const names = sharing
      .map((other) =>
        other.app.app === appView.app
          ? `"${other.action.label}"`
          : `"${other.action.label}" (${other.app.display_name})`,
      )
      .join(", ");
    return m(
      "p",
      { class: "keybinding-shared type-helper mt-0.5 text-warning" },
      `Shares a shortcut with ${names}; edit one of them so each shortcut runs one thing.`,
    );
  }

  /** The warnings of the shortcuts a row shows (the editor shows its own while the row is being edited). */
  function rowWarnings(appView: KeybindingAppView, chords: readonly string[]): m.Children {
    return chords
      .flatMap((chord) => noticesOf(appView, chord))
      .map((warning) => m("p", { class: "keybinding-row-warning type-helper mt-0.5 text-warning" }, warning));
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
    const isChrome = isImbueStudio(appView);
    const saveError = isChrome ? (chromeErrorByActionId.get(action.id) ?? null) : null;
    const isResetShown = isChrome ? differsFromDefaults(action, context) : isResetOffered(action, context);
    return m("div", { key: action.id, "data-keybinding-action": `${appView.app}.${action.id}` }, [
      m("div", { class: "flex min-h-10 items-center gap-3 border-b border-default py-2" }, [
        m("div", { class: "min-w-0 flex-1" }, [
          m("span", { class: "type-body text-primary" }, action.label),
          saveError === null
            ? null
            : m("p", { class: "keybinding-save-error type-helper mt-0.5 text-danger" }, `Not saved: ${saveError}`),
          sharedShortcutWarning(appView, action),
          isEditing ? null : rowWarnings(appView, chords),
        ]),
        rowMarker(appView, action, isPendingReplace),
        m("span", { class: "shrink-0" }, shortcutsView(chords, hidden)),
        m("span", { class: "flex shrink-0 items-center gap-1" }, [
          isResetShown
            ? m(
                Button,
                {
                  sm: true,
                  variant: "ghost",
                  extra: "keybinding-reset",
                  disabled: isBusy || editor !== null || pendingRevert !== null,
                  ...hoverTooltipAttrs(isChrome ? "Go back to the default" : "Undo your change in this workspace"),
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
    oninit(vnode) {
      void load();
      vnode.attrs.onChordRelay?.(recordRelayedChord);
    },
    onremove(vnode) {
      closeEditor();
      vnode.attrs.onChordRelay?.(null);
    },
    view(vnode) {
      onRecordingChange = vnode.attrs.onRecordingChange;
      onSaveChromeShortcut = vnode.attrs.onSaveChromeShortcut;
      if (vnode.attrs.context !== undefined) context = vnode.attrs.context;
      const sentChromeShortcuts = vnode.attrs.chromeShortcuts ?? null;
      if (sentChromeShortcuts !== chromeShortcuts) receiveChromeShortcuts(sentChromeShortcuts);
      if (loadError !== null) {
        return m("p", { class: "type-helper text-danger" }, `The shortcuts could not be loaded: ${loadError}`);
      }
      if (catalog === null) return m("p", { class: "type-helper text-secondary" }, "Loading the shortcuts...");
      const loaded = catalog;
      const shown = shownCatalog() ?? loaded;
      return m("div", { "data-keyboard-settings": true }, [
        unsentBanner(loaded),
        notice !== null ? m("p", { class: "keybinding-notice type-helper mb-3 text-secondary" }, notice) : null,
        m(
          "div",
          { class: "keybinding-apps" },
          shown.apps.filter((appView) => appView.actions.length > 0).map((appView) => appSection(appView)),
        ),
      ]);
    },
  };
}
