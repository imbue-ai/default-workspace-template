import { describe, expect, it } from "vitest";

import { makeAgentWorkingReports } from "./agentWorkingReports";

function reports(): { told: boolean[] } & ReturnType<typeof makeAgentWorkingReports<string>> {
  const told: boolean[] = [];
  return { told, ...makeAgentWorkingReports<string>((isAnyWorking) => told.push(isAnyWorking)) };
}

describe("makeAgentWorkingReports", () => {
  it("says nothing while no page has had an agent working", () => {
    const { told, note, forget, isAnyWorking } = reports();

    note("chat-a", false);
    forget("chat-b");

    expect(told).toEqual([]);
    expect(isAnyWorking()).toBe(false);
  });

  it("says an agent is working from one page's start to its end, each change once", () => {
    const { told, note } = reports();

    note("chat-a", true);
    note("chat-a", true);
    note("chat-a", false);
    note("chat-a", false);

    expect(told).toEqual([true, false]);
  });

  it("keeps saying so while another page still has an agent working", () => {
    const { told, note, isAnyWorking } = reports();

    note("chat-a", true);
    note("chat-b", true);
    note("chat-b", false);
    expect(told).toEqual([true]);
    expect(isAnyWorking()).toBe(true);

    note("chat-a", false);
    expect(told).toEqual([true, false]);
  });

  it("drops a page's word with the page, and stops saying so once that was the last", () => {
    const { told, note, forget } = reports();

    note("chat-a", true);
    note("chat-b", true);
    forget("chat-a");
    expect(told).toEqual([true]);

    forget("chat-b");
    expect(told).toEqual([true, false]);

    // A page that comes back starts from having said nothing.
    note("chat-b", true);
    expect(told).toEqual([true, false, true]);
  });
});
