// @vitest-environment jsdom
/**
 * The update notice banner over the fake shell: what it says for each state of the notice the socket
 * delivers, which verbs it offers, the confirmation before a rollback, and how a refusal is shown.
 */
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import type { UpdateNoticeWire } from "../model/records";
import { DesktopStore } from "../store/DesktopStore";
import { FakeDesktopApi, FakeDesktopSocket } from "../testing/fakeShell";
import { appRecord, desktopRecord, noticeWire, themeMetricsRecord } from "../testing/records";
import { SYSTEM_SERVICES_RESTART_DETAILS, UpdateNoticeBanner } from "./UpdateNoticeBanner";

const CLIENT = "client-1";
const NO_LINK = { desktopId: null, open: null, launch: null };

let api: FakeDesktopApi;
let socket: FakeDesktopSocket;
let store: DesktopStore;

/** A fetch that answers every request the way the shell's notice routes do, recording what was asked. */
function stubFetch(status = 204, detail?: string): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async () => ({
    ok: status < 400,
    status,
    json: async () => (detail === undefined ? {} : { detail }),
  }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function requestedPaths(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls.map(([url]) => String(url));
}

/** The socket delivers the notice, as the shell's seed and every ``update_notice_changed`` do. */
function deliverNotice(wire: UpdateNoticeWire | null): void {
  socket.deliver().onUpdateNoticeChanged(wire);
  m.redraw.sync();
}

function mountBanner(): HTMLElement {
  return mountView(() => m(UpdateNoticeBanner, { store }));
}

function click(root: ParentNode, selector: string): void {
  const element = root.querySelector(selector);
  if (element === null) throw new Error(`nothing matches ${selector}`);
  element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
}

async function settled(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  m.redraw.sync();
}

beforeEach(async () => {
  api = new FakeDesktopApi();
  socket = new FakeDesktopSocket();
  api.desktops = [desktopRecord("home")];
  store = new DesktopStore({
    clientId: CLIENT,
    api,
    socket,
    metrics: themeMetricsRecord(),
    modes: { isCompact: false, isTouch: false },
    redraw: () => m.redraw(),
    notify: () => undefined,
    reloadInterface: () => undefined,
  });
  await store.start(NO_LINK);
  socket
    .deliver()
    .onAppsUpdated([appRecord("chat", { display_name: "Chat" }), appRecord("terminal", { display_name: "Terminal" })]);
});

afterEach(() => {
  unmountViews();
  vi.unstubAllGlobals();
});

describe("UpdateNoticeBanner", () => {
  it("renders nothing without a notice", () => {
    expect(mountBanner().querySelector(".update-notice-banner")).toBeNull();
  });

  it("names every app the apply touched, since a rollback takes them all back together", () => {
    const root = mountBanner();
    deliverNotice(noticeWire(["chat"]));
    expect(root.querySelector(".update-notice-banner")?.textContent).toContain(
      "Chat was updated a moment ago. If something is not working, you can go back to the previous version.",
    );

    deliverNotice(noticeWire(["chat", "system_interface"]));
    expect(root.querySelector(".update-notice-banner")?.textContent).toContain(
      "Chat and the workspace interface were updated a moment ago.",
    );

    deliverNotice(noticeWire([]));
    expect(root.querySelector(".update-notice-banner")?.textContent).toContain(
      "The workspace was updated a moment ago.",
    );

    deliverNotice(null);
    expect(root.querySelector(".update-notice-banner")).toBeNull();
  });

  it("offers the two verbs on an open notice, and Everything seems good confirms it", async () => {
    const fetchMock = stubFetch();
    const root = mountBanner();
    deliverNotice(noticeWire(["chat"]));
    expect(root.querySelector(".update-notice-rollback")).not.toBeNull();

    click(root, ".update-notice-confirm");
    await settled();

    expect(requestedPaths(fetchMock)).toEqual(["/api/updates/pending/confirm"]);
  });

  it("shows the shell's refusal in the banner and keeps the verbs", async () => {
    stubFetch(403, "This is a preview of a proposed change; it cannot change the live workspace.");
    const root = mountBanner();
    deliverNotice(noticeWire(["chat"]));

    click(root, ".update-notice-confirm");
    await settled();

    expect(root.querySelector(".update-notice-banner-error")?.textContent).toContain("preview");
    expect(root.querySelector(".update-notice-confirm")).not.toBeNull();
  });

  it("drops a refusal once the notice moves on to the state that explains it", async () => {
    // A second window's Roll back, refused because the first's rollback is already running: the
    // refusal stands beside the open notice, and goes when that rollback's progress arrives.
    stubFetch(409, "A rollback is already running.");
    const root = mountBanner();
    deliverNotice(noticeWire(["chat"]));

    click(root, ".update-notice-rollback");
    m.redraw.sync();
    click(root, ".destroy-dialog-btn-destroy");
    await settled();
    expect(root.querySelector(".update-notice-banner-error")?.textContent).toContain("already running");

    deliverNotice(noticeWire(["chat"], { progress: "Reverting the update" }));

    expect(root.querySelector(".update-notice-banner")?.textContent).toContain("Reverting the update");
    expect(root.querySelector(".update-notice-banner-error")).toBeNull();
  });

  it("asks before rolling back, naming the apps and the programs, then posts the rollback", async () => {
    const fetchMock = stubFetch(202, "The rollback has started.");
    const root = mountBanner();
    deliverNotice(noticeWire(["chat", "system_interface"]));

    click(root, ".update-notice-rollback");
    m.redraw.sync();

    const dialog = root.querySelector(".modal-message");
    expect(dialog?.textContent).toContain("Chat and the workspace interface");
    expect(dialog?.textContent).toContain("chat and system_interface will restart");
    expect(root.textContent).not.toContain(SYSTEM_SERVICES_RESTART_DETAILS);
    expect(requestedPaths(fetchMock)).toEqual([]);

    click(root, ".destroy-dialog-btn-cancel");
    m.redraw.sync();
    expect(root.querySelector(".modal-message")).toBeNull();
    expect(requestedPaths(fetchMock)).toEqual([]);

    click(root, ".update-notice-rollback");
    m.redraw.sync();
    click(root, ".destroy-dialog-btn-destroy");
    await settled();

    expect(requestedPaths(fetchMock)).toEqual(["/api/updates/pending/rollback"]);
    expect(root.querySelector(".modal-message")).toBeNull();
  });

  it("warns in the dialog when the rollback leaves the restart to an agent", () => {
    const root = mountBanner();
    deliverNotice(noticeWire(["chat"], { needs_system_services_restart: true }));
    click(root, ".update-notice-rollback");
    m.redraw.sync();
    expect(root.textContent).toContain(SYSTEM_SERVICES_RESTART_DETAILS);
  });

  it("names the workspace in the dialog when the apply touched no app", () => {
    const root = mountBanner();
    deliverNotice(noticeWire([], { needs_system_services_restart: true }));
    click(root, ".update-notice-rollback");
    m.redraw.sync();

    const dialog = root.querySelector(".modal-message");
    expect(dialog?.textContent).toContain("the workspace");
    expect(dialog?.textContent).toContain("no app restarts on its own");
    expect(root.textContent).toContain(SYSTEM_SERVICES_RESTART_DETAILS);
  });

  it("shows a running rollback's progress with no verbs, then the outcome with Close", async () => {
    const fetchMock = stubFetch();
    const root = mountBanner();
    deliverNotice(noticeWire(["chat"], { progress: "Restoring the previous version" }));
    expect(root.querySelector(".update-notice-banner")?.textContent).toContain("Restoring the previous version");
    expect(root.querySelector("button")).toBeNull();

    deliverNotice(noticeWire(["chat"], { outcome: "Rolled back to the previous version." }));
    expect(root.querySelector(".update-notice-banner")?.textContent).toContain("Rolled back to the previous version.");
    expect(root.querySelector(".update-notice-rollback")).toBeNull();

    click(root, ".update-notice-close");
    await settled();
    expect(requestedPaths(fetchMock)).toEqual(["/api/updates/pending/confirm"]);
  });
});
