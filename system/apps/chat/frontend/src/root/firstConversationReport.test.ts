import { describe, expect, it } from "vitest";

import { makeFirstConversationReport } from "./firstConversationReport";

function report(): { told: boolean[] } & ReturnType<typeof makeFirstConversationReport> {
  const told: boolean[] = [];
  return { told, ...makeFirstConversationReport((isPending) => told.push(isPending)) };
}

describe("makeFirstConversationReport", () => {
  it("says nothing until a chat list has arrived, then says what it said", () => {
    const { told, note } = report();

    note(null);
    expect(told).toEqual([]);

    note(false);
    expect(told).toEqual([false]);
  });

  it("tells a first conversation that is following its tree, and tells its end once", () => {
    const { told, note } = report();

    note(true);
    note(true);
    note(false);
    note(false);

    expect(told).toEqual([true, false]);
  });

  it("tells a shell that greets the root what it was told before, since that shell has heard nothing", () => {
    const { told, note, forgetTold } = report();

    note(true);
    forgetTold();
    note(true);
    expect(told).toEqual([true, true]);

    note(false);
    forgetTold();
    note(false);
    expect(told).toEqual([true, true, false, false]);
  });

  it("keeps a greeting that comes before any chat list for the list's arrival", () => {
    const { told, note, forgetTold } = report();

    forgetTold();
    note(null);
    note(true);

    expect(told).toEqual([true]);
  });
});
