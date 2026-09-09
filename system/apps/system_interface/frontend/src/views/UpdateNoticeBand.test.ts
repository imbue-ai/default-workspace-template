// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { applyApps } from "../models/Inventory";
import { applyUpdateNotice, resetUpdateNoticeForTesting } from "../models/UpdateNotice";
import { noticeWire } from "../models/UpdateNotice.test";
import { appRecord } from "../testing/records";
import { IframePanel } from "./IframePanel";
import {
  OPEN_NOTICE_TEXT,
  OPEN_SHELL_NOTICE_TEXT,
  SERVICES_RESTART_DETAILS,
  UpdateNoticeBand,
} from "./UpdateNoticeBand";
import { UpdateNoticeBanner } from "./UpdateNoticeBanner";

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

function mountBand(appName: string): { root: HTMLElement; redraw: () => void } {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const redraw = (): void => {
    m.render(root, m(UpdateNoticeBand, { appName }));
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

describe("UpdateNoticeBand", () => {
  it("renders nothing without a notice, or for an app the apply did not touch", () => {
    expect(mountBand("chat").root.querySelector(".update-notice-band")).toBeNull();
    applyUpdateNotice(noticeWire({ apps: ["chat"] }));
    expect(mountBand("terminal").root.querySelector(".update-notice-band")).toBeNull();
    expect(mountBand("chat").root.querySelector(".update-notice-band")).not.toBeNull();
  });

  it("offers the two verbs on an open notice, and Everything seems good confirms it", async () => {
    const fetchMock = stubFetch();
    applyUpdateNotice(noticeWire({ apps: ["chat"] }));
    const { root } = mountBand("chat");
    expect(root.querySelector(".update-notice-band")?.textContent).toContain(OPEN_NOTICE_TEXT);
    expect(root.querySelector(".update-notice-rollback")).not.toBeNull();

    click(root, ".update-notice-confirm");
    await settled();

    expect(requestedPaths(fetchMock)).toEqual(["/api/updates/pending/confirm"]);
  });

  it("shows the shell's refusal in the band and keeps the verbs", async () => {
    stubFetch(403, "This is a preview of a proposed change; it cannot change the live workspace.");
    applyUpdateNotice(noticeWire({ apps: ["chat"] }));
    const { root, redraw } = mountBand("chat");

    click(root, ".update-notice-confirm");
    await settled();
    redraw();

    expect(root.querySelector(".update-notice-band-error")?.textContent).toContain("preview");
    expect(root.querySelector(".update-notice-confirm")).not.toBeNull();
  });

  it("asks before rolling back, naming the apps and the programs, then posts the rollback", async () => {
    const fetchMock = stubFetch(202, "The rollback has started.");
    applyUpdateNotice(noticeWire({ apps: ["chat", "system_interface"], programs: ["chat", "system_interface"] }));
    const { root, redraw } = mountBand("chat");

    click(root, ".update-notice-rollback");
    redraw();

    const dialog = root.querySelector(".modal-message");
    expect(dialog?.textContent).toContain("Chat and system_interface");
    expect(dialog?.textContent).toContain("chat and system_interface will restart");
    expect(root.textContent).not.toContain(SERVICES_RESTART_DETAILS);
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
    applyUpdateNotice(noticeWire({ apps: ["chat"], needs_services_restart: true }));
    const { root, redraw } = mountBand("chat");
    click(root, ".update-notice-rollback");
    redraw();
    expect(root.textContent).toContain(SERVICES_RESTART_DETAILS);
  });

  it("shows a running rollback's progress with no verbs, then the outcome with Close", async () => {
    const fetchMock = stubFetch();
    applyUpdateNotice(noticeWire({ apps: ["chat"], progress: "Restoring the previous version" }));
    const { root, redraw } = mountBand("chat");
    expect(root.querySelector(".update-notice-band")?.textContent).toContain("Restoring the previous version");
    expect(root.querySelector("button")).toBeNull();

    applyUpdateNotice(noticeWire({ apps: ["chat"], outcome: "Rolled back to the previous version." }));
    redraw();
    expect(root.querySelector(".update-notice-band")?.textContent).toContain("Rolled back to the previous version.");
    expect(root.querySelector(".update-notice-rollback")).toBeNull();

    click(root, ".update-notice-close");
    await settled();
    expect(requestedPaths(fetchMock)).toEqual(["/api/updates/pending/confirm"]);
  });
});

describe("UpdateNoticeBanner", () => {
  it("is the shell's own band, keyed on the shell's name", () => {
    const root = document.createElement("div");
    document.body.appendChild(root);
    m.render(root, m(UpdateNoticeBanner));
    expect(root.querySelector(".update-notice-banner")).toBeNull();

    applyUpdateNotice(noticeWire({ apps: ["system_interface"] }));
    m.render(root, m(UpdateNoticeBanner));
    expect(root.querySelector(".update-notice-banner")?.textContent).toContain(OPEN_SHELL_NOTICE_TEXT);
  });
});

describe("IframePanel with the notice", () => {
  it("carries the band above the frame without reloading the frame when the notice arrives", () => {
    const root = document.createElement("div");
    document.body.appendChild(root);
    const render = (): void => {
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
    };
    render();
    const frame = root.querySelector("iframe");
    expect(frame).not.toBeNull();
    expect(root.querySelector(".update-notice-band")).toBeNull();

    applyUpdateNotice(noticeWire({ apps: ["chat"] }));
    render();

    expect(root.querySelector(".update-notice-band")).not.toBeNull();
    expect(root.querySelector("iframe")).toBe(frame);
    // The band sits above the frame in the panel's column.
    const panel = root.querySelector(".si-iframe-panel");
    expect(panel?.firstElementChild?.classList.contains("update-notice-band")).toBe(true);
  });
});
