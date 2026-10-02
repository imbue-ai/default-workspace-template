import { describe, expect, it } from "vitest";
import { item } from "../testing/records";
import {
  chatStateLine,
  formatBytes,
  formatDuration,
  harnessName,
  headlineFor,
  isStopSuggested,
  pressureNotice,
  programStateLine,
} from "./format";

const NOW_MS = 1_790_000_000_000;
const MIB = 1024 * 1024;

describe("sizes and durations", () => {
  it("reads in GB above a gigabyte, MB below, and <1 MB for a sliver", () => {
    expect(formatBytes(3.4 * 1024 * MIB)).toBe("3.4 GB");
    expect(formatBytes(336 * MIB)).toBe("336 MB");
    expect(formatBytes(1000)).toBe("<1 MB");
    expect(formatBytes(0)).toBe("0 MB");
  });

  it("rounds idle time to the unit a person would say", () => {
    expect(formatDuration(30)).toBe("just now");
    expect(formatDuration(600)).toBe("10 min");
    expect(formatDuration(2 * 3600)).toBe("2 hr");
    expect(formatDuration(86400)).toBe("1 day");
    expect(formatDuration(3 * 86400)).toBe("3 days");
  });
});

describe("chat and app lines", () => {
  it("names harnesses as people know them", () => {
    expect(harnessName("pi-coding")).toBe("Pi");
    expect(harnessName("opencode")).toBe("OpenCode");
    expect(harnessName("something-new")).toBe("something-new");
    expect(harnessName(null)).toBeNull();
  });

  it("says how long an idle chat has waited, and never 'just now ago'", () => {
    const waiting = item({ item_id: "c1", name: "A", last_messaged_at: NOW_MS / 1000 - 7200 });
    expect(chatStateLine(waiting, NOW_MS)).toBe("Waiting for you · last message 2 hr ago");
    const fresh = item({ item_id: "c2", name: "B", last_messaged_at: NOW_MS / 1000 - 5 });
    expect(chatStateLine(fresh, NOW_MS)).toBe("Waiting for you · last message just now");
    expect(chatStateLine(item({ item_id: "c3", name: "C", state: "stopped" }), NOW_MS)).toContain("starts again");
  });

  it("suggests stopping only a running chat idle for a quarter of an hour or more", () => {
    const idle = item({ item_id: "c1", name: "A", rss_kib: 1000, last_messaged_at: NOW_MS / 1000 - 16 * 60 });
    expect(isStopSuggested(idle, NOW_MS)).toBe(true);
    expect(isStopSuggested({ ...idle, last_messaged_at: NOW_MS / 1000 - 10 * 60 }, NOW_MS)).toBe(false);
    expect(isStopSuggested({ ...idle, state: "working" }, NOW_MS)).toBe(false);
    expect(isStopSuggested({ ...idle, rss_kib: 0 }, NOW_MS)).toBe(false);
  });

  it("gives an always-on app its own reason and an on-demand app its lifecycle", () => {
    const terminal = item({
      item_id: "app:terminal",
      name: "Terminal",
      kind: "APP",
      state: "RUNNING",
      description: "Command line",
      always_on_reason: "Kept ready in case something needs fixing",
    });
    expect(programStateLine(terminal)).toBe("Command line · Kept ready in case something needs fixing");
    const files = item({
      item_id: "app:files",
      name: "Files",
      kind: "APP",
      state: "STOPPED",
      is_on_demand: true,
      is_restarted_on_open: true,
    });
    expect(programStateLine(files)).toBe("Not running · starts when you open it");
    const unknown = item({ item_id: "app:chat", name: "Chat", kind: "APP", state: "UNKNOWN", description: "Chats" });
    expect(programStateLine(unknown)).toBe("Chats · state unknown right now");
    // The browser is never offered Stop, but the desktop still starts it again after a Quit from its window menu.
    const browser = item({
      item_id: "app:browser",
      name: "Browser",
      kind: "APP",
      state: "STOPPED",
      is_restarted_on_open: true,
    });
    expect(programStateLine(browser)).toBe("Not running · starts when you open it");
    const exited = item({ item_id: "app:old", name: "Old", kind: "APP", state: "EXITED" });
    expect(programStateLine(exited)).toBe("Not running (exited)");
  });

  it("says an agent outside any chat is running, with what it is", () => {
    const helper = item({
      item_id: "agent:w",
      name: "test-runner",
      kind: "HELPER_AGENT",
      state: "running",
      description: "A helper",
    });
    expect(chatStateLine(helper, NOW_MS)).toBe("Running · A helper");
  });
});

describe("headline", () => {
  it("names where closing starts once memory is critical", () => {
    const headline = headlineFor({
      limit_bytes: 8 * 1024 * MIB,
      used_bytes: 7.4 * 1024 * MIB,
      status: "CRITICAL",
      source: "CGROUP",
      source_detail: "",
      closes_at_used_bytes: 7.2 * 1024 * MIB,
      closer: "MEMORY_GUARD",
      min_available_percent: 10,
      closing_detail: "",
    });
    expect(headline.tone).toBe("danger");
    expect(headline.body).toContain("less than 10% is free, the workspace's memory guard closes");
  });

  it("says the system closes things when the container's limit acts first", () => {
    const headline = headlineFor({
      limit_bytes: 8 * 1024 * MIB,
      used_bytes: 7.9 * 1024 * MIB,
      status: "CRITICAL",
      source: "CGROUP",
      source_detail: "",
      closes_at_used_bytes: 8 * 1024 * MIB,
      closer: "SYSTEM_LIMIT",
      min_available_percent: null,
      closing_detail: "",
    });
    expect(headline.body).toContain("When memory is completely full, the system closes");
  });
});

describe("the memory pressure notice", () => {
  const NOW = Date.parse("2026-10-02T10:30:00Z");

  it("says for how long memory has stayed tight while it lasts", () => {
    const notice = pressureNotice(
      { started_at: "2026-10-02T10:12:00Z", last_tight_at: "2026-10-02T10:30:00Z", peak_kib: 1, is_ongoing: true },
      NOW,
    );
    expect(notice?.isOngoing).toBe(true);
    expect(notice?.title).toBe("Memory has stayed tight for 18 min");
    expect(notice?.body).toContain("Stopping a chat or app you aren't using makes room.");
  });

  it("counts an eased stretch to the end of its last minute, and says nothing when there was none", () => {
    const notice = pressureNotice(
      { started_at: "2026-10-02T09:00:00Z", last_tight_at: "2026-10-02T09:24:00Z", peak_kib: 1, is_ongoing: false },
      NOW,
    );
    expect(notice?.isOngoing).toBe(false);
    expect(notice?.title).toBe("Memory was tight for 25 min earlier");
    expect(pressureNotice(null, NOW)).toBeNull();
  });
});
