import { describe, expect, it } from "vitest";
import { intakeTokenFromSearch, rootPathFor, selectionFromSearch } from "./selection";

describe("selection", () => {
  it("reads the selected chat off the query and refuses an id that cannot be a chat's", () => {
    expect(selectionFromSearch("?chat=agent-0123abc")).toBe("agent-0123abc");
    expect(selectionFromSearch("?chat=not%20an%20id")).toBeNull();
    expect(selectionFromSearch("")).toBeNull();
  });

  it("reads the pending intake's token beside the selection, null for none", () => {
    expect(intakeTokenFromSearch("?chat=agent-1&intake=tok-1")).toBe("tok-1");
    expect(intakeTokenFromSearch("?intake=tok-2")).toBe("tok-2");
    expect(intakeTokenFromSearch("?chat=agent-1")).toBeNull();
    expect(intakeTokenFromSearch("?intake=")).toBeNull();
    expect(intakeTokenFromSearch("")).toBeNull();
  });

  it("writes the selection back as the root's path, with no token", () => {
    expect(rootPathFor("agent-0123abc")).toBe("/?chat=agent-0123abc");
    expect(rootPathFor(null)).toBe("/");
  });
});
