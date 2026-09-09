import { afterEach, describe, expect, it } from "vitest";

import { dispatchSocketEventForTesting } from "./Inventory";
import {
  getUpdateNotice,
  isNoticeSettled,
  isRollbackRunning,
  resetUpdateNoticeForTesting,
  updateNoticeForApp,
} from "./UpdateNotice";
import type { UpdateNoticeWire } from "./UpdateNotice";

export function noticeWire(overrides: Partial<UpdateNoticeWire> = {}): UpdateNoticeWire {
  return {
    merge_sha: "abc1234abc1234abc1234abc1234abc1234abc12",
    applied_at: 1_780_000_000,
    driven_by: "mngr/update-widgets",
    apps: ["chat"],
    programs: ["chat"],
    needs_services_restart: false,
    progress: null,
    outcome: null,
    ...overrides,
  };
}

afterEach(() => {
  resetUpdateNoticeForTesting();
});

describe("the update notice over the socket", () => {
  it("is what the shell last pushed, and gone once the shell pushes null", () => {
    expect(getUpdateNotice()).toBeNull();
    dispatchSocketEventForTesting({
      type: "update_notice_changed",
      notice: noticeWire({ apps: ["chat", "terminal"] }),
    });
    expect(getUpdateNotice()?.apps).toEqual(["chat", "terminal"]);
    expect(getUpdateNotice()?.drivenBy).toBe("mngr/update-widgets");
    dispatchSocketEventForTesting({ type: "update_notice_changed", notice: null });
    expect(getUpdateNotice()).toBeNull();
  });

  it("answers only for the apps the apply touched", () => {
    dispatchSocketEventForTesting({ type: "update_notice_changed", notice: noticeWire({ apps: ["chat"] }) });
    expect(updateNoticeForApp("chat")).not.toBeNull();
    expect(updateNoticeForApp("terminal")).toBeNull();
    expect(updateNoticeForApp("system_interface")).toBeNull();
  });

  it("tells a running rollback from a settled one", () => {
    dispatchSocketEventForTesting({ type: "update_notice_changed", notice: noticeWire({ progress: "Restarting" }) });
    expect(isRollbackRunning(getUpdateNotice()!)).toBe(true);
    expect(isNoticeSettled(getUpdateNotice()!)).toBe(false);
    dispatchSocketEventForTesting({
      type: "update_notice_changed",
      notice: noticeWire({ outcome: "Rolled back to the previous version." }),
    });
    expect(isRollbackRunning(getUpdateNotice()!)).toBe(false);
    expect(isNoticeSettled(getUpdateNotice()!)).toBe(true);
  });
});
