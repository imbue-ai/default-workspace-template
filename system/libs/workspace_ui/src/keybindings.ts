/**
 * The keybinding chord grammar on the browser side: one canonical spelling for a keystroke, matching
 * the `workspace_profile` library's grammar (lowercase tokens joined with `+`, modifiers in the fixed
 * ctrl/alt/shift/meta order, named keys as their lowercased `KeyboardEvent.key` values, the literal
 * `+` spelled `plus`). Apps turn a keydown into a chord with `chordFromKeydown` and look it up in the
 * chords their backend resolved from `/api/keybindings/me`.
 *
 * The spelling is platform-neutral, its reading is not (the keyboard-shortcuts spec in the mngr repo,
 * section 4): `meta` is the primary modifier, the Command key on a Mac and the Control key on Windows
 * and Linux, where the Windows key is no modifier at all; `ctrl` is the Mac's Control key, which has
 * no partner elsewhere, so a chord holding it is hidden off a Mac. The per-platform lists below mirror
 * the desktop's `workspace_profile_contract.json` the way the two Python grammars do (the contract
 * test pins them): the chords no binding may claim, the ones a browser keeps from its pages, the ones
 * an OS keeps from every app, and the ones the desktop app must intercept for a binding to win.
 */

/** The resolved chords of one app's actions, as its `/api/keybindings/me` route answers them. */
export interface ResolvedKeybindings {
  app: string;
  chords_by_action: Record<string, string[]>;
}

/** The keys a chord is read from; structurally a `KeyboardEvent`, so tests can pass a plain object. */
export interface ChordKeyEvent {
  key: string;
  /** The physical key (`KeyN`), read instead of `key` for a letter or digit while Option is held. */
  code?: string;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  metaKey: boolean;
}

/** The platform a chord is read on: which physical key `meta` and `ctrl` name, and which chords the OS keeps. */
export type ChordPlatform = "mac" | "windows" | "linux";

/** Where the workspace runs: the desktop app, or a page in a browser (which keeps its own shortcuts from pages). */
export type ChordSurface = "desktop" | "browser";

/** What decides whether a chord can arrive, and what to warn about when it is recorded. */
export interface ChordContext {
  readonly platform: ChordPlatform;
  readonly surface: ChordSurface;
}

/** A chord a layer above the workspace keeps, with what it is used for ("the app switcher"). */
export interface KeptChord {
  readonly chord: string;
  readonly reason: string;
}

const NAMED_KEYS = new Set([
  "enter",
  "escape",
  "tab",
  "space",
  "backspace",
  "delete",
  "insert",
  "home",
  "end",
  "pageup",
  "pagedown",
  "arrowup",
  "arrowdown",
  "arrowleft",
  "arrowright",
  "plus",
  "f1",
  "f2",
  "f3",
  "f4",
  "f5",
  "f6",
  "f7",
  "f8",
  "f9",
  "f10",
  "f11",
  "f12",
]);

/** The chords no binding may claim on any platform: Ctrl+Alt+Del, which Windows keeps for itself. */
export const REFUSED_CHORDS: readonly string[] = ["alt+meta+delete"];

/** The chords a browser keeps from its pages, so they never arrive when the workspace runs in one. */
export const BROWSER_RESERVED_CHORDS: Readonly<Record<ChordPlatform, readonly string[]>> = {
  mac: [
    "meta+t",
    "shift+meta+t",
    "meta+n",
    "shift+meta+n",
    "meta+w",
    "shift+meta+w",
    "ctrl+tab",
    "ctrl+shift+tab",
    "shift+meta+}",
    "shift+meta+{",
    "alt+meta+arrowleft",
    "alt+meta+arrowright",
    "meta+q",
    "meta+h",
    "alt+meta+h",
    "meta+m",
  ],
  windows: [
    "meta+t",
    "shift+meta+t",
    "meta+n",
    "shift+meta+n",
    "meta+w",
    "shift+meta+w",
    "meta+tab",
    "shift+meta+tab",
    "meta+pageup",
    "meta+pagedown",
    "meta+f4",
  ],
  linux: [
    "meta+t",
    "shift+meta+t",
    "meta+n",
    "shift+meta+n",
    "meta+w",
    "shift+meta+w",
    "meta+tab",
    "shift+meta+tab",
    "meta+pageup",
    "meta+pagedown",
    "meta+f4",
    "shift+meta+q",
  ],
};

/** The chords an OS keeps from every app, so they may never arrive there (on a Mac, until System Settings says otherwise). */
export const OS_KEPT_CHORDS: Readonly<Record<ChordPlatform, readonly KeptChord[]>> = {
  mac: [
    { chord: "meta+tab", reason: "the app switcher" },
    { chord: "shift+meta+tab", reason: "the app switcher" },
    { chord: "meta+space", reason: "Spotlight" },
    { chord: "alt+meta+space", reason: "Spotlight" },
    { chord: "ctrl+space", reason: "switching input sources" },
    { chord: "ctrl+alt+space", reason: "switching input sources" },
    { chord: "shift+meta+#", reason: "screenshots" },
    { chord: "shift+meta+$", reason: "screenshots" },
    { chord: "shift+meta+%", reason: "screenshots" },
    { chord: "ctrl+arrowup", reason: "Mission Control" },
    { chord: "ctrl+arrowdown", reason: "Mission Control" },
    { chord: "ctrl+arrowleft", reason: "switching Spaces" },
    { chord: "ctrl+arrowright", reason: "switching Spaces" },
    { chord: "ctrl+meta+q", reason: "locking the screen" },
    { chord: "alt+meta+escape", reason: "Force Quit" },
    { chord: "alt+meta+d", reason: "hiding the Dock" },
  ],
  windows: [
    { chord: "alt+tab", reason: "the window switcher" },
    { chord: "alt+shift+tab", reason: "the window switcher" },
    { chord: "alt+escape", reason: "window cycling" },
    { chord: "meta+escape", reason: "the Start menu" },
    { chord: "shift+meta+escape", reason: "Task Manager" },
  ],
  linux: [
    { chord: "alt+tab", reason: "the window switcher" },
    { chord: "alt+shift+tab", reason: "the window switcher" },
    { chord: "alt+escape", reason: "window cycling" },
    { chord: "alt+f1", reason: "the activities overview" },
    { chord: "alt+f2", reason: "the run dialog" },
    { chord: "alt+meta+t", reason: "opening a terminal" },
    { chord: "alt+meta+l", reason: "locking the screen" },
    { chord: "alt+meta+arrowup", reason: "switching workspaces" },
    { chord: "alt+meta+arrowdown", reason: "switching workspaces" },
    { chord: "alt+meta+arrowleft", reason: "switching workspaces" },
    { chord: "alt+meta+arrowright", reason: "switching workspaces" },
    { chord: "alt+f4", reason: "closing the window" },
    { chord: "alt+space", reason: "the window menu" },
  ],
};

/** The chords that reach the desktop app and that the OS then acts on unless the app intercepts them. */
export const HOST_ACTED_CHORDS: Readonly<Record<ChordPlatform, readonly KeptChord[]>> = {
  mac: [],
  windows: [
    { chord: "alt+f4", reason: "closing the window" },
    { chord: "alt+space", reason: "the window menu" },
  ],
  linux: [],
};

const OS_NAMES: Readonly<Record<ChordPlatform, string>> = { mac: "macOS", windows: "Windows", linux: "Linux" };

/** The platform's name as a sentence says it ("macOS", "Windows", "Linux"). */
export function chordPlatformName(platform: ChordPlatform): string {
  return OS_NAMES[platform];
}

/** The platform the browser runs on, from what it says about itself. */
export function detectChordPlatform(nav: { platform: string; userAgent: string } = navigator): ChordPlatform {
  if (/Mac|iPhone|iPad|iPod/.test(nav.platform) || /Mac OS/.test(nav.userAgent)) return "mac";
  if (/Linux|X11/.test(nav.platform) || /Linux/.test(nav.userAgent)) return "linux";
  return "windows";
}

/** Whether the browser runs on an Apple platform, where chords read with the Mac modifier symbols. */
export function isApplePlatform(): boolean {
  return detectChordPlatform() === "mac";
}

// A letter or digit key, as `KeyboardEvent.code` names it.
const LETTER_OR_DIGIT_CODE = /^(?:Key([A-Z])|Digit([0-9]))$/;

/**
 * The canonical chord one keydown spells on the platform, or null for a keystroke that is no chord (a
 * bare modifier, a key the grammar has no name for, or off a Mac the Windows key).
 *
 * Off a Mac, the Control key spells `meta`, the grammar's primary modifier. While Option is held a Mac
 * composes a character for a letter or digit (Option-N gives a tilde), so those keys are read from the
 * physical key instead, on every platform, and the chord records the same everywhere.
 */
export function chordFromKeydown(
  event: ChordKeyEvent,
  platform: ChordPlatform = detectChordPlatform(),
): string | null {
  const raw = event.key;
  if (raw === "" || raw === "Control" || raw === "Alt" || raw === "Shift" || raw === "Meta") {
    return null;
  }
  const isMac = platform === "mac";
  if (!isMac && event.metaKey) return null;
  let key: string;
  const physical = event.altKey && event.code !== undefined ? LETTER_OR_DIGIT_CODE.exec(event.code) : null;
  if (physical !== null) key = (physical[1] ?? physical[2]).toLowerCase();
  else if (raw === " ") key = "space";
  else if (raw === "+") key = "plus";
  else if (raw.length === 1) key = raw.toLowerCase();
  else {
    key = raw.toLowerCase();
    if (!NAMED_KEYS.has(key)) return null;
  }
  const tokens: string[] = [];
  if (isMac && event.ctrlKey) tokens.push("ctrl");
  if (event.altKey) tokens.push("alt");
  if (event.shiftKey) tokens.push("shift");
  if (isMac ? event.metaKey : event.ctrlKey) tokens.push("meta");
  tokens.push(key);
  return tokens.join("+");
}

/** Whether the keystroke spells one of the given chords on the platform. */
export function keydownMatchesChords(
  event: ChordKeyEvent,
  chords: readonly string[],
  platform: ChordPlatform = detectChordPlatform(),
): boolean {
  if (chords.length === 0) return false;
  const chord = chordFromKeydown(event, platform);
  return chord !== null && chords.includes(chord);
}

/** Whether the chord is one no binding may claim. */
export function isRefusedChord(chord: string): boolean {
  return REFUSED_CHORDS.includes(chord);
}

const FUNCTION_KEY = /^f([1-9]|1[0-2])$/;

/**
 * Whether a canonical chord may fire anywhere in the workspace: it holds a modifier other than Shift alone, or is a
 * function key, so typing into any app never triggers it (mirrors `workspace_profile.keybindings`).
 */
export function isWorkspaceWideChord(chord: string): boolean {
  const tokens = chord.split("+");
  const key = tokens[tokens.length - 1];
  return FUNCTION_KEY.test(key) || tokens.slice(0, -1).some((modifier) => modifier !== "shift");
}

function holdsControl(chord: string): boolean {
  return chord.split("+").includes("ctrl");
}

/**
 * Whether the chord cannot arrive in the context, so it is hidden there: a chord holding the Mac's
 * Control key off a Mac, or a chord the browser keeps from its pages when the workspace runs in one.
 */
export function isChordHiddenIn(chord: string, context: ChordContext): boolean {
  if (context.platform !== "mac" && holdsControl(chord)) return true;
  return context.surface === "browser" && BROWSER_RESERVED_CHORDS[context.platform].includes(chord);
}

/**
 * What to tell the user about a chord in the context, none of it blocking: that a browser would keep
 * it from the workspace, that the OS is known to keep it from every app, or that the desktop app takes
 * it from the OS to run the binding.
 */
export function chordNotices(chord: string, context: ChordContext): string[] {
  const isApple = context.platform === "mac";
  const label = chordDisplayLabel(chord, isApple);
  const os = OS_NAMES[context.platform];
  const notices: string[] = [];
  if (context.surface === "desktop" && BROWSER_RESERVED_CHORDS[context.platform].includes(chord)) {
    notices.push(
      `${label} is a shortcut the browser keeps for itself, so this won't work when the workspace is open in a browser.`,
    );
  }
  const kept = OS_KEPT_CHORDS[context.platform].find((entry) => entry.chord === chord);
  if (kept !== undefined) {
    const remedy = isApple ? " unless you change that in System Settings" : "";
    notices.push(`${os} uses ${label} for ${kept.reason}, so this may never reach Imbue Studio${remedy}.`);
  }
  const acted = HOST_ACTED_CHORDS[context.platform].find((entry) => entry.chord === chord);
  if (acted !== undefined) {
    notices.push(`On ${os}, ${label} is normally used for ${acted.reason}; here it runs this action instead.`);
  }
  return notices;
}

interface KeyLabels {
  appleKeycap: string;
  appleWord: string;
  otherKeycap: string;
  otherWord: string;
}

function sameOnEveryPlatform(keycap: string, word: string): KeyLabels {
  return { appleKeycap: keycap, appleWord: word, otherKeycap: keycap, otherWord: word };
}

// Apple's symbols for the modifiers and the keys its menus draw as symbols; other platforms spell them out,
// where `meta` is the Control key.
const LABELS_BY_TOKEN: Record<string, KeyLabels> = {
  ctrl: { appleKeycap: "⌃", appleWord: "Control", otherKeycap: "Ctrl", otherWord: "Ctrl" },
  alt: { appleKeycap: "⌥", appleWord: "Option", otherKeycap: "Alt", otherWord: "Alt" },
  shift: { appleKeycap: "⇧", appleWord: "Shift", otherKeycap: "Shift", otherWord: "Shift" },
  meta: { appleKeycap: "⌘", appleWord: "Command", otherKeycap: "Ctrl", otherWord: "Ctrl" },
  enter: { appleKeycap: "↩", appleWord: "Return", otherKeycap: "Enter", otherWord: "Enter" },
  tab: { appleKeycap: "⇥", appleWord: "Tab", otherKeycap: "Tab", otherWord: "Tab" },
  backspace: { appleKeycap: "⌫", appleWord: "Delete", otherKeycap: "Backspace", otherWord: "Backspace" },
  delete: { appleKeycap: "⌦", appleWord: "Forward Delete", otherKeycap: "Del", otherWord: "Delete" },
  escape: sameOnEveryPlatform("Esc", "Escape"),
  space: sameOnEveryPlatform("Space", "Space"),
  insert: sameOnEveryPlatform("Ins", "Insert"),
  home: sameOnEveryPlatform("Home", "Home"),
  end: sameOnEveryPlatform("End", "End"),
  pageup: sameOnEveryPlatform("PgUp", "Page Up"),
  pagedown: sameOnEveryPlatform("PgDn", "Page Down"),
  arrowup: sameOnEveryPlatform("↑", "Up Arrow"),
  arrowdown: sameOnEveryPlatform("↓", "Down Arrow"),
  arrowleft: sameOnEveryPlatform("←", "Left Arrow"),
  arrowright: sameOnEveryPlatform("→", "Right Arrow"),
  plus: sameOnEveryPlatform("+", "Plus"),
};

function labelsOfToken(token: string): KeyLabels {
  const named = LABELS_BY_TOKEN[token];
  if (named !== undefined) return named;
  // A function key or a printable key: the grammar keeps it lowercase, a keycap shows it as printed.
  const printed = token.toUpperCase();
  return sameOnEveryPlatform(printed, printed);
}

/** The keycaps one canonical chord is drawn as, modifiers first in the grammar's order. */
export function chordKeycapLabels(chord: string, isApple: boolean): string[] {
  return chord.split("+").map((token) => {
    const labels = labelsOfToken(token);
    return isApple ? labels.appleKeycap : labels.otherKeycap;
  });
}

/**
 * One canonical chord as a single label, the way a menu writes a shortcut: Apple's symbols run together on an
 * Apple platform ("⇧⌘N", the grammar's modifier order being Apple's own), the names joined with `+` elsewhere
 * ("Ctrl+Shift+N").
 */
export function chordDisplayLabel(chord: string, isApple: boolean): string {
  return chordKeycapLabels(chord, isApple).join(isApple ? "" : "+");
}

/** One canonical chord in words ("Command+Shift+N"), for tooltips, screen readers, and messages. */
export function chordSpokenLabel(chord: string, isApple: boolean): string {
  return chord
    .split("+")
    .map((token) => {
      const labels = labelsOfToken(token);
      return isApple ? labels.appleWord : labels.otherWord;
    })
    .join("+");
}

/** Whether the keystroke triggers the named action under the resolved chords (none loaded: never). */
export function keydownMatchesAction(
  event: ChordKeyEvent,
  resolved: ResolvedKeybindings | null,
  actionId: string,
  platform: ChordPlatform = detectChordPlatform(),
): boolean {
  if (resolved === null) return false;
  return keydownMatchesChords(event, resolved.chords_by_action[actionId] ?? [], platform);
}
