// @vitest-environment jsdom
/**
 * The keyboard shortcuts page's wiring of the chrome's messages to the Keyboard settings section: every
 * `minds:chrome-shortcuts` answer reaches the section in a render of its own, since the section settles one pending
 * save per answer it renders.
 */
import { describe, expect, it, vi } from "vitest";

const handlers = new Map<string, (message: Record<string, unknown>) => void>();
const renderedChromeShortcuts: unknown[] = [];

vi.mock("../style.css", () => ({}));
vi.mock("@imbue/workspace-ui/src/embed", () => ({
  CHROME_SHORTCUTS: "minds:chrome-shortcuts",
  EMBEDDER_CAPABILITIES: "minds:embedder-capabilities",
  KEYBOARD_SHORTCUTS_PAGE: "minds:keyboard-shortcuts-page",
  RECORD_CHORD: "minds:record-chord",
  SAVE_CHROME_SHORTCUT: "minds:save-chrome-shortcut",
  sendToEmbedder: vi.fn(),
  setEmbedderMessageHandler: (type: string, handler: (message: Record<string, unknown>) => void) =>
    handlers.set(type, handler),
}));
vi.mock("../views/KeyboardSettingsSection", () => ({
  KeyboardSettingsSection: () => ({
    view: (vnode: { attrs: { chromeShortcuts: unknown } }) => {
      renderedChromeShortcuts.push(vnode.attrs.chromeShortcuts);
      return null;
    },
  }),
}));

const QUIT = { id: "quit", label: "Quit", defaultChords: ["meta+q"], chords: ["meta+q"] };

describe("the keyboard shortcuts page", () => {
  it("renders each of the chrome's shortcut answers, however close together they arrive", async () => {
    document.body.innerHTML = '<div id="app"></div>';
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe(): void {}
      },
    );
    await import("./index");
    const receiveChromeShortcuts = handlers.get("minds:chrome-shortcuts");
    expect(receiveChromeShortcuts).toBeDefined();

    receiveChromeShortcuts?.({ actions: [{ ...QUIT, chords: [] }] });
    receiveChromeShortcuts?.({ actions: [QUIT], error: "The account is offline" });

    expect(renderedChromeShortcuts.slice(-2)).toEqual([
      { actions: [{ ...QUIT, chords: [] }], error: null },
      { actions: [QUIT], error: "The account is offline" },
    ]);
  });
});
