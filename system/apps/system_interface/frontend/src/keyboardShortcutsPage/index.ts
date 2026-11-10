/**
 * The keyboard shortcuts page, served at `/keyboard-shortcuts`: the Keyboard settings section on a page of its own,
 * which Imbue Studio's Workspace settings frames. The page tells Imbue Studio its height, which the frame takes so
 * the settings pane scrolls it with everything else, and whether its recorder is listening, while which Imbue
 * Studio closes nothing on Escape and runs no shortcut, handing the recorder instead the keys the OS would act on
 * (`minds:record-chord`). Its first word also says the page has loaded, which tells a workspace that serves the page
 * from one too old to (whose shell answers the path with the desktop).
 *
 * The chrome says what it is with `minds:embedder-capabilities`, as it does to the main workspace frame: one that
 * can pull a window out is the desktop app, any other is a browser, which keeps its own shortcuts from the page.
 * Until it says, the page reads as the desktop app, which hides nothing.
 */

import "../style.css";
import m from "mithril";
import {
  EMBEDDER_CAPABILITIES,
  KEYBOARD_SHORTCUTS_PAGE,
  RECORD_CHORD,
  sendToEmbedder,
  setEmbedderMessageHandler,
} from "@imbue/workspace-ui/src/embed";
import { type ChordContext, detectChordPlatform } from "@imbue/workspace-ui/src/keybindings";
import { KeyboardSettingsSection } from "../views/KeyboardSettingsSection";

function start(): void {
  const root = document.getElementById("app");
  if (root === null) return;
  let isRecording = false;
  let lastSentHeight = -1;
  let lastSentIsRecording = false;
  let context: ChordContext = { platform: detectChordPlatform(), surface: "desktop" };
  let recordRelayedChord: ((chord: string) => void) | null = null;

  function reportState(): void {
    const height = Math.ceil(root?.getBoundingClientRect().height ?? 0);
    if (height === lastSentHeight && isRecording === lastSentIsRecording) return;
    lastSentHeight = height;
    lastSentIsRecording = isRecording;
    sendToEmbedder(KEYBOARD_SHORTCUTS_PAGE, { height, isRecording });
  }

  setEmbedderMessageHandler(EMBEDDER_CAPABILITIES, (message) => {
    context = { ...context, surface: message.canPopOut === true ? "desktop" : "browser" };
    m.redraw();
  });
  setEmbedderMessageHandler(RECORD_CHORD, (message) => {
    if (typeof message.chord === "string") recordRelayedChord?.(message.chord);
  });

  m.mount(root, {
    view: () =>
      m("main", { class: "keyboard-shortcuts-page pb-2 text-primary" }, [
        m(KeyboardSettingsSection, {
          context,
          onRecordingChange: (isNowRecording: boolean) => {
            isRecording = isNowRecording;
            reportState();
          },
          onChordRelay: (record) => {
            recordRelayedChord = record;
          },
        }),
      ]),
  });
  new ResizeObserver(reportState).observe(root);
  reportState();
}

start();
