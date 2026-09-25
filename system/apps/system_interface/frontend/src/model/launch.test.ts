import { describe, expect, it } from "vitest";
import {
  MAX_WINDOW_PATH_LENGTH,
  appLaunchesOf,
  fillParamOf,
  freeTextParams,
  freeTextRowsOf,
  launchPathOf,
  launchRowKindOf,
  orderAppLaunches,
  textRowDisabledReason,
} from "./launch";
import { appRecord, launchPathRecord } from "../testing/records";

const docs = appRecord("docs", {
  launcher_rank: 20,
  launch_paths: [launchPathRecord({ id: "new", label: "New docs", params: ["message"], text_param: "message" })],
});
const notes = appRecord("notes", { launcher_rank: 10 });
const plain = appRecord("plain", { launch_paths: [launchPathRecord({ id: "open", label: "Open plain", path: "/" })] });
const hidden = appRecord("hidden", { internal: true });
const buddy = appRecord("buddy", {
  pin: { path: "/", style: "plain", scope: "independent", default_mode: "bar" },
  launch_paths: [
    launchPathRecord({ id: "root", label: "Buddy", path: "/" }),
    launchPathRecord({ id: "new", label: "New buddy", path: "/new", params: ["message"], text_param: "message" }),
    launchPathRecord({ id: "send", label: "Send to buddy...", path: "/", params: ["send"], text_param: "send" }),
    launchPathRecord({
      id: "draft",
      label: "Draft into buddy",
      path: "/api/intake",
      method: "POST",
      params: ["message"],
      presets: { target: "current_chat", is_draft: "true" },
      draft_param: "message",
    }),
    launchPathRecord({ id: "posted-root", label: "Posted root", path: "/", method: "POST" }),
  ],
});

describe("app launches", () => {
  it("lists every launch path of every openable app, ranked apps first", () => {
    const launches = orderAppLaunches(appLaunchesOf([plain, docs, hidden, notes]));
    expect(launches.map((launch) => `${launch.app.name}:${launch.launchPath.id}`)).toEqual([
      "notes:new",
      "docs:new",
      "plain:open",
    ]);
  });

  it("classifies a launch path as text before focus, and focus only for a GET at the pin's path", () => {
    expect(launchRowKindOf(buddy, buddy.launch_paths[0])).toBe("focus");
    expect(launchRowKindOf(buddy, buddy.launch_paths[1])).toBe("text");
    // A text param wins over the pin's path: the row is a free-text row, never a second focus row.
    expect(launchRowKindOf(buddy, buddy.launch_paths[2])).toBe("text");
    // A draft param makes a free-text row too.
    expect(launchRowKindOf(buddy, buddy.launch_paths[3])).toBe("text");
    // A POST at the pin's path starts something, so it is never the focus row.
    expect(launchRowKindOf(buddy, buddy.launch_paths[4])).toBe("new");
    expect(launchRowKindOf(plain, plain.launch_paths[0])).toBe("new");
    expect(launchRowKindOf(docs, docs.launch_paths[0])).toBe("text");
  });

  it("lists the free-text rows in launcher order, then manifest order", () => {
    expect(freeTextRowsOf([buddy, docs, notes]).map((launch) => `${launch.app.name}:${launch.launchPath.id}`)).toEqual(
      ["docs:new", "buddy:new", "buddy:send", "buddy:draft"],
    );
    expect(freeTextRowsOf([plain, notes])).toEqual([]);
  });
});

describe("launch paths of an app", () => {
  it("finds a launch path by id", () => {
    expect(launchPathOf(docs, "new")?.label).toBe("New docs");
    expect(launchPathOf(docs, "other")).toBeNull();
  });
});

describe("the free-text params", () => {
  const launchPath = launchPathRecord({ path: "/new", params: ["message"], text_param: "message" });
  const drafting = launchPathRecord({
    path: "/api/intake",
    method: "POST",
    params: ["message"],
    presets: { is_draft: "true" },
    draft_param: "message",
  });

  it("fills the text param, else the draft param, and nothing for empty text", () => {
    expect(fillParamOf(launchPath)).toBe("message");
    expect(fillParamOf(drafting)).toBe("message");
    expect(fillParamOf(launchPathRecord())).toBeNull();
    expect(freeTextParams(launchPath, "new chat")).toEqual({ message: "new chat" });
    expect(freeTextParams(drafting, "Draw me")).toEqual({ message: "Draw me" });
    expect(freeTextParams(launchPath, "")).toEqual({});
    expect(freeTextParams(launchPathRecord(), "hi")).toEqual({});
  });

  it("stands a GET launch path down on the encoded path's length, not the typed length", () => {
    const fitting = "x".repeat(MAX_WINDOW_PATH_LENGTH - "/new?message=".length);
    expect(textRowDisabledReason(launchPath, fitting)).toBeNull();
    expect(textRowDisabledReason(launchPath, `${fitting}x`)).toBe("Too long to send from here");
    // Encoding triples a non-ASCII character, so a shorter text can be over the bound.
    expect(textRowDisabledReason(launchPath, "é".repeat(700))).toBe("Too long to send from here");
    // The presets go into the query too, and count.
    const preset = launchPathRecord({ ...launchPath, presets: { target: "x".repeat(20) } });
    expect(textRowDisabledReason(preset, fitting)).toBe("Too long to send from here");
  });

  it("never stands a POST launch path down for length, and stands one with no text param down outright", () => {
    expect(textRowDisabledReason(drafting, "x".repeat(5000))).toBeNull();
    expect(textRowDisabledReason(launchPathRecord(), "hi")).toBe("No app on this machine can start a chat");
  });
});
