import { describe, expect, it } from "vitest";
import { item, summary } from "../testing/records";
import { chatDraftFor, questionsFor, rowLabel } from "./questions";

const BROWSER = item({ item_id: "app:browser", name: "Browser", kind: "APP", state: "RUNNING", rss_kib: 800 * 1024 });
const WALLPAPER = item({ item_id: "chat:c1", name: "Wallpaper", rss_kib: 330 * 1024 });

describe("questions", () => {
  it("asks what uses the most while there is room, answered with the real top two", () => {
    const questions = questionsFor(summary("COMFORTABLE", [BROWSER, WALLPAPER], "chat:c1"));
    expect(questions.map((question) => question.id)).toEqual(["most-memory", "more-memory", "why-first"]);
    expect(questions[0].answer[0]).toBe('Right now, "Browser" uses the most (800 MB), then "Wallpaper" (330 MB).');
  });

  it("asks what happens and how to free memory once it is tight", () => {
    const questions = questionsFor(summary("TIGHT", [WALLPAPER], null));
    expect(questions.map((question) => question.id)).toEqual(["fills-up", "free-up", "more-memory"]);
  });

  it("does not ask why something closes first when nothing is marked", () => {
    expect(questionsFor(summary("COMFORTABLE", [WALLPAPER], null)).map((question) => question.id)).not.toContain(
      "why-first",
    );
  });
});

describe("the chat draft", () => {
  const current = summary("COMFORTABLE", [BROWSER, WALLPAPER], "chat:c1");

  it("leads with the question, so drafts stacked in the composer read differently", () => {
    const first = chatDraftFor(current, "Can I get more memory?");
    const second = chatDraftFor(current, "What happens if it fills up?");
    expect(first.startsWith("Can I get more memory?")).toBe(true);
    expect(second.startsWith("What happens if it fills up?")).toBe(true);
  });

  it("carries the page's figures and its likely-first pick", () => {
    const draft = chatDraftFor(current, "Why would that one be closed first?");
    expect(draft).toContain('"Your workspace has room to spare." (3.4 GB of 8.0 GB in use)');
    expect(draft).toContain('marks "Wallpaper" as likely to be closed first');
    expect(draft).toContain("I'm looking at System Monitor");
  });

  it("asks for help understanding the page when the user names no question", () => {
    expect(chatDraftFor(current, null).endsWith("Can you help me understand what I'm seeing?")).toBe(true);
  });
});

describe("row labels", () => {
  it("say what a row is, its size, its state, and its closing risk", () => {
    expect(rowLabel(WALLPAPER, "Waiting for you", true, "10%")).toBe(
      "Wallpaper, 330 MB, 10% of memory in use, Waiting for you, likely closed first if memory runs out",
    );
    expect(rowLabel({ ...WALLPAPER, rss_kib: 0 }, "Stopped", false, "0%")).toBe("Wallpaper, not running, Stopped");
  });
});
