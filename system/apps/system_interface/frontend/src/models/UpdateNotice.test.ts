import { afterEach, describe, expect, it } from "vitest";

import { noticeWire } from "../testing/records";
import { dispatchSocketEventForTesting } from "./Inventory";
import {
  getUpdateNotice,
  isNoticeSettled,
  isRollbackRunning,
  resetUpdateNoticeForTesting,
  updateNoticeForApp,
} from "./UpdateNotice";

afterEach(() => {
  resetUpdateNoticeForTesting();
});

describe("the update notice over the socket", () => {
  it("is what the shell last pushed, and gone once the shell pushes null", () => {
    expect(getUpdateNotice()).toBeNull();
    dispatchSocketEventForTesting({
      type: "update_notice_changed",
      notice: noticeWire(["chat", "terminal"]),
    });
    expect(getUpdateNotice()?.apps).toEqual(["chat", "terminal"]);
    expect(getUpdateNotice()?.drivenBy).toBe("mngr/update-widgets");
    dispatchSocketEventForTesting({ type: "update_notice_changed", notice: null });
    expect(getUpdateNotice()).toBeNull();
  });

  it("answers only for the apps the apply touched", () => {
    dispatchSocketEventForTesting({ type: "update_notice_changed", notice: noticeWire(["chat"]) });
    expect(updateNoticeForApp("chat")).not.toBeNull();
    expect(updateNoticeForApp("terminal")).toBeNull();
    expect(updateNoticeForApp("system_interface")).toBeNull();
  });

  it("goes to the shell's banner when the apply touched no app at all", () => {
    // A change to how the workspace starts touches no program or bundle; no tab carries a band,
    // and without the banner the person would have no way to open the rollback dialog.
    dispatchSocketEventForTesting({ type: "update_notice_changed", notice: noticeWire([]) });
    expect(updateNoticeForApp("system_interface")).not.toBeNull();
    expect(updateNoticeForApp("chat")).toBeNull();
  });

  it("tells a running rollback from a settled one", () => {
    dispatchSocketEventForTesting({
      type: "update_notice_changed",
      notice: noticeWire([], { progress: "Restarting" }),
    });
    expect(isRollbackRunning(getUpdateNotice()!)).toBe(true);
    expect(isNoticeSettled(getUpdateNotice()!)).toBe(false);
    dispatchSocketEventForTesting({
      type: "update_notice_changed",
      notice: noticeWire([], { outcome: "Rolled back to the previous version." }),
    });
    expect(isRollbackRunning(getUpdateNotice()!)).toBe(false);
    expect(isNoticeSettled(getUpdateNotice()!)).toBe(true);
  });
});
