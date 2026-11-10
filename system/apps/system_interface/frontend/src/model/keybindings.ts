/**
 * The resolved chords of the shell's own actions, from its backend. Fetched at boot and again on
 * every `keybindings_changed` broadcast; until the first answer lands, each caller's fallback
 * chords (the manifest's defaults) answer, so the launcher is never briefly dead, and the
 * workspace-wide actions have none.
 *
 * The workspace-wide actions (the keyboard-shortcuts spec in the mngr repo, section 6) are the
 * shell's actions with `scope = "workspace"` in its `app.toml`: their chords run them wherever focus
 * is in the workspace, so the shell matches them itself, hands them to every page it frames, and
 * reports them to the desktop app.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import {
  type ChordKeyEvent,
  type ChordPlatform,
  chordFromKeydown,
  detectChordPlatform,
  isChordHiddenOn,
  keydownMatchesChords,
  type ResolvedKeybindings,
} from "@imbue/workspace-ui/src/keybindings";
import type { KeybindingsCatalog } from "./api";

/** The shell's workspace-wide actions, as its `app.toml` declares them with `scope = "workspace"`. */
export const WORKSPACE_ACTION_IDS = [
  "close-window",
  "next-window",
  "previous-window",
  "open-launcher",
  "show-shortcuts",
  "open-overview",
] as const;

export type WorkspaceActionId = (typeof WORKSPACE_ACTION_IDS)[number];

export function isWorkspaceActionId(value: unknown): value is WorkspaceActionId {
  return typeof value === "string" && (WORKSPACE_ACTION_IDS as readonly string[]).includes(value);
}

let resolved: ResolvedKeybindings | null = null;
let platformOverride: ChordPlatform | null = null;

export async function refreshShellKeybindings(): Promise<void> {
  try {
    const response = await fetch(apiUrl("/api/keybindings/me"));
    if (!response.ok) return;
    resolved = (await response.json()) as ResolvedKeybindings;
  } catch {
    // The chords stay as last loaded; the next change broadcast retries.
  }
}

/** The platform the shell reads chords on: the browser's, unless a test named one. */
export function shellChordPlatform(): ChordPlatform {
  return platformOverride ?? detectChordPlatform();
}

export function matchesShellKeybinding(
  event: ChordKeyEvent,
  actionId: string,
  fallbackChords: readonly string[],
): boolean {
  const chords = resolved?.chords_by_action[actionId] ?? fallbackChords;
  return keydownMatchesChords(event, chords, shellChordPlatform());
}

/** The chords of each workspace-wide action that can arrive on the shell's platform. */
export function workspaceChordsByAction(): Record<WorkspaceActionId, string[]> {
  const platform = shellChordPlatform();
  const chordsOf = (actionId: WorkspaceActionId): string[] =>
    (resolved?.chords_by_action[actionId] ?? []).filter((chord) => !isChordHiddenOn(chord, platform));
  return Object.fromEntries(WORKSPACE_ACTION_IDS.map((actionId) => [actionId, chordsOf(actionId)])) as Record<
    WorkspaceActionId,
    string[]
  >;
}

/** The workspace-wide action a chord runs, or null. */
export function workspaceActionOfChord(chord: string): WorkspaceActionId | null {
  const chordsByAction = workspaceChordsByAction();
  return WORKSPACE_ACTION_IDS.find((actionId) => chordsByAction[actionId].includes(chord)) ?? null;
}

/** The workspace-wide action a keystroke in the shell's own document runs, or null. */
export function workspaceActionOfKeydown(event: ChordKeyEvent): WorkspaceActionId | null {
  const chord = chordFromKeydown(event, shellChordPlatform());
  return chord === null ? null : workspaceActionOfChord(chord);
}

/** Every active chord of every app's actions in the catalog that can arrive on the shell's platform. */
export function claimedChordsOf(catalog: KeybindingsCatalog): string[] {
  const platform = shellChordPlatform();
  const chords = catalog.apps.flatMap((app) => app.actions.flatMap((action) => action.chords));
  return [...new Set(chords)].filter((chord) => !isChordHiddenOn(chord, platform));
}

/** For tests: pretend the backend answered these chords. */
export function setResolvedShellKeybindingsForTest(value: ResolvedKeybindings | null): void {
  resolved = value;
}

/** For tests: read chords as this platform would, whatever the test runner's browser says. */
export function setShellChordPlatformForTest(platform: ChordPlatform | null): void {
  platformOverride = platform;
}
