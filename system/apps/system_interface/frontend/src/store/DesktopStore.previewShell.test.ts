// @vitest-environment jsdom
/**
 * The store in a preview shell (a second shell booted over a copy of the live state to show a proposed
 * change): the verbs whose effect would land on the live workspace are withheld.
 */
import "../testing/dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { FakeDesktopApi, FakeDesktopSocket } from "../testing/fakeShell";
import { markPageAsPreviewShell } from "../testing/previewShell";
import { appRecord, desktopRecord, themeMetricsRecord } from "../testing/records";
import { DesktopStore } from "./DesktopStore";

const NO_LINK = { desktopId: null, open: null, launch: null };

let socket: FakeDesktopSocket;
let store: DesktopStore;
let unmarkPreview: (() => void) | null = null;

beforeEach(async () => {
  const api = new FakeDesktopApi();
  socket = new FakeDesktopSocket();
  api.desktops = [desktopRecord("home")];
  store = new DesktopStore({
    clientId: "client-1",
    api,
    socket,
    metrics: themeMetricsRecord(),
    modes: { isCompact: false, isTouch: false },
    redraw: () => undefined,
    notify: () => undefined,
    reloadInterface: () => undefined,
  });
  await store.start(NO_LINK);
  socket.deliver().onAppsUpdated([appRecord("docs", { program: "docs" })]);
});

afterEach(() => {
  unmarkPreview?.();
  unmarkPreview = null;
});

describe("a preview shell", () => {
  it("offers no Stop or Start for an app the live shell would, since they reach the live supervisord", () => {
    const docs = store.getState().apps[0];
    expect(store.canStopApp(docs)).toBe(true);

    unmarkPreview = markPageAsPreviewShell();

    expect(store.canStopApp(docs)).toBe(false);
  });
});
