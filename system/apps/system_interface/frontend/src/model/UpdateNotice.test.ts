import { describe, expect, it } from "vitest";

import { noticeWire } from "../testing/records";
import { isNoticeSettled, isRollbackRunning, isWorkspaceOnlyNotice, noticeFromWire } from "./UpdateNotice";

describe("the update notice's states", () => {
  it("tells an open notice from a running rollback and a settled one", () => {
    const open = noticeFromWire(noticeWire(["chat", "terminal"]));
    expect(open.apps).toEqual(["chat", "terminal"]);
    expect(isRollbackRunning(open)).toBe(false);
    expect(isNoticeSettled(open)).toBe(false);

    const running = noticeFromWire(noticeWire([], { progress: "Restarting" }));
    expect(isRollbackRunning(running)).toBe(true);
    expect(isNoticeSettled(running)).toBe(false);

    const settled = noticeFromWire(
      noticeWire([], { progress: "Restarting", outcome: "Rolled back to the previous version." }),
    );
    expect(isRollbackRunning(settled)).toBe(false);
    expect(isNoticeSettled(settled)).toBe(true);
  });

  it("reads an apply that touched no app as a change to the workspace itself", () => {
    expect(isWorkspaceOnlyNotice(noticeFromWire(noticeWire([])))).toBe(true);
    expect(isWorkspaceOnlyNotice(noticeFromWire(noticeWire(["system_interface"])))).toBe(false);
  });
});
