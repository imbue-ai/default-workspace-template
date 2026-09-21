// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { applyApps } from "../models/Inventory";
import { applyUpdateNotice, resetUpdateNoticeForTesting } from "../models/UpdateNotice";
import { appRecord, noticeWire } from "../testing/records";
import { IframePanel } from "./IframePanel";
import { SYSTEM_SERVICES_RESTART_DETAILS, UpdateNoticeBanner } from "./UpdateNoticeBanner";

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

function mountBanner(): { root: HTMLElement; redraw: () => void } {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const redraw = (): void => {
    m.render(root, m(UpdateNoticeBanner));
  };
  redraw();
  return { root, redraw };
}

function click(root: ParentNode, selector: string): void {
  const element = root.querySelector(selector);
  if (element === null) throw new Error(`nothing matches ${selector}`);
  element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
}

async function settled(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
  document.body.innerHTML = "";
  applyApps([appRecord("chat", { display_name: "Chat" }), appRecord("terminal", { display_name: "Terminal" })]);
});

afterEach(() => {
  resetUpdateNoticeForTesting();
  vi.unstubAllGlobals();
});

describe("UpdateNoticeBanner", () => {
  it("renders nothing without a notice", () => {
    expect(mountBanner().root.querySelector(".update-notice-banner")).toBeNull();
  });

  it("names every app the apply touched, since a rollback takes them all back together", () => {
    applyUpdateNotice(noticeWire(["chat"]));
    expect(mountBanner().root.querySelector(".update-notice-banner")?.textContent).toContain(
      "Chat was updated a moment ago. If something is not working, you can go back to the previous version.",
    );

    applyUpdateNotice(noticeWire(["chat", "system_interface"]));
    expect(mountBanner().root.querySelector(".update-notice-banner")?.textContent).toContain(
      "Chat and the workspace interface were updated a moment ago.",
    );

    applyUpdateNotice(noticeWire([]));
    expect(mountBanner().root.querySelector(".update-notice-banner")?.textContent).toContain(
      "The workspace was updated a moment ago.",
    );
  });

  it("offers the two verbs on an open notice, and Everything seems good confirms it", async () => {
    const fetchMock = stubFetch();
    applyUpdateNotice(noticeWire(["chat"]));
    const { root } = mountBanner();
    expect(root.querySelector(".update-notice-rollback")).not.toBeNull();

    click(root, ".update-notice-confirm");
    await settled();

    expect(requestedPaths(fetchMock)).toEqual(["/api/updates/pending/confirm"]);
  });

  it("shows the shell's refusal in the banner and keeps the verbs", async () => {
    stubFetch(403, "This is a preview of a proposed change; it cannot change the live workspace.");
    applyUpdateNotice(noticeWire(["chat"]));
    const { root, redraw } = mountBanner();

    click(root, ".update-notice-confirm");
    await settled();
    redraw();

    expect(root.querySelector(".update-notice-banner-error")?.textContent).toContain("preview");
    expect(root.querySelector(".update-notice-confirm")).not.toBeNull();
  });

  it("drops a refusal once the notice moves on to the state that explains it", async () => {
    // A second window's Roll back, refused because the first's rollback is already running: the
    // refusal stands beside the open notice, and goes when that rollback's progress arrives.
    stubFetch(409, "A rollback is already running.");
    applyUpdateNotice(noticeWire(["chat"]));
    const { root, redraw } = mountBanner();

    click(root, ".update-notice-rollback");
    redraw();
    click(root, ".destroy-dialog-btn-destroy");
    await settled();
    redraw();
    expect(root.querySelector(".update-notice-banner-error")?.textContent).toContain("already running");

    applyUpdateNotice(noticeWire(["chat"], { progress: "Reverting the update" }));
    redraw();

    expect(root.querySelector(".update-notice-banner")?.textContent).toContain("Reverting the update");
    expect(root.querySelector(".update-notice-banner-error")).toBeNull();
  });

  it("asks before rolling back, naming the apps and the programs, then posts the rollback", async () => {
    const fetchMock = stubFetch(202, "The rollback has started.");
    applyUpdateNotice(noticeWire(["chat", "system_interface"]));
    const { root, redraw } = mountBanner();

    click(root, ".update-notice-rollback");
    redraw();

    const dialog = root.querySelector(".modal-message");
    expect(dialog?.textContent).toContain("Chat and the workspace interface");
    expect(dialog?.textContent).toContain("chat and system_interface will restart");
    expect(root.textContent).not.toContain(SYSTEM_SERVICES_RESTART_DETAILS);
    expect(requestedPaths(fetchMock)).toEqual([]);

    click(root, ".destroy-dialog-btn-cancel");
    redraw();
    expect(root.querySelector(".modal-message")).toBeNull();
    expect(requestedPaths(fetchMock)).toEqual([]);

    click(root, ".update-notice-rollback");
    redraw();
    click(root, ".destroy-dialog-btn-destroy");
    await settled();
    redraw();

    expect(requestedPaths(fetchMock)).toEqual(["/api/updates/pending/rollback"]);
    expect(root.querySelector(".modal-message")).toBeNull();
  });

  it("warns in the dialog when the rollback leaves the restart to an agent", () => {
    applyUpdateNotice(noticeWire(["chat"], { needs_system_services_restart: true }));
    const { root, redraw } = mountBanner();
    click(root, ".update-notice-rollback");
    redraw();
    expect(root.textContent).toContain(SYSTEM_SERVICES_RESTART_DETAILS);
  });

  it("names the workspace in the dialog when the apply touched no app", () => {
    applyUpdateNotice(noticeWire([], { needs_system_services_restart: true }));
    const { root, redraw } = mountBanner();
    click(root, ".update-notice-rollback");
    redraw();

    const dialog = root.querySelector(".modal-message");
    expect(dialog?.textContent).toContain("the workspace");
    expect(dialog?.textContent).toContain("no app restarts on its own");
    expect(root.textContent).toContain(SYSTEM_SERVICES_RESTART_DETAILS);
  });

  it("shows a running rollback's progress with no verbs, then the outcome with Close", async () => {
    const fetchMock = stubFetch();
    applyUpdateNotice(noticeWire(["chat"], { progress: "Restoring the previous version" }));
    const { root, redraw } = mountBanner();
    expect(root.querySelector(".update-notice-banner")?.textContent).toContain("Restoring the previous version");
    expect(root.querySelector("button")).toBeNull();

    applyUpdateNotice(noticeWire(["chat"], { outcome: "Rolled back to the previous version." }));
    redraw();
    expect(root.querySelector(".update-notice-banner")?.textContent).toContain("Rolled back to the previous version.");
    expect(root.querySelector(".update-notice-rollback")).toBeNull();

    click(root, ".update-notice-close");
    await settled();
    expect(requestedPaths(fetchMock)).toEqual(["/api/updates/pending/confirm"]);
  });
});

describe("the notice and an app's frame", () => {
  it("leaves the notice to the banner: a frame of a touched app carries none of its own", () => {
    applyUpdateNotice(noticeWire(["chat"]));
    const root = document.createElement("div");
    document.body.appendChild(root);
    m.render(
      root,
      m(IframePanel, {
        url: "http://chat.example/agent-1",
        title: "Chat 1",
        appName: "chat",
        address: "app:chat?instance=agent-1",
        contract: { address: "app:chat?instance=agent-1", tabId: "t1", viewId: "everything", isVisible: true },
      }),
    );
    expect(root.querySelector("iframe")).not.toBeNull();
    expect(root.querySelector(".update-notice-rollback")).toBeNull();
    expect(root.textContent).not.toContain("updated a moment ago");
  });
});
