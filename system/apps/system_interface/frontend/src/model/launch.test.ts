import { describe, expect, it } from "vitest";
import {
  MAX_WINDOW_PATH_LENGTH,
  appLaunchesOf,
  freeTextRowsOf,
  chatPath,
  isPathShowingChat,
  launchPathOf,
  launchPathWithParams,
  launchRowKindOf,
  orderAppLaunches,
  textPathOf,
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
    launchPathRecord({ id: "root", label: "Buddy", path: "/", params: ["draft"] }),
    launchPathRecord({ id: "new", label: "New buddy", path: "/new", params: ["message"], text_param: "message" }),
    launchPathRecord({ id: "send", label: "Send to buddy...", path: "/", params: ["send"], text_param: "send" }),
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

  it("classifies a launch path as text before focus, and focus only at the pin's path", () => {
    expect(launchRowKindOf(buddy, buddy.launch_paths[0])).toBe("focus");
    expect(launchRowKindOf(buddy, buddy.launch_paths[1])).toBe("text");
    // A text param wins over the pin's path: the row is a free-text row, never a second focus row.
    expect(launchRowKindOf(buddy, buddy.launch_paths[2])).toBe("text");
    expect(launchRowKindOf(plain, plain.launch_paths[0])).toBe("new");
    expect(launchRowKindOf(docs, docs.launch_paths[0])).toBe("text");
  });

  it("lists the free-text rows in launcher order, then manifest order", () => {
    expect(freeTextRowsOf([buddy, docs, notes]).map((launch) => `${launch.app.name}:${launch.launchPath.id}`)).toEqual(
      ["docs:new", "buddy:new", "buddy:send"],
    );
    expect(freeTextRowsOf([plain, notes])).toEqual([]);
  });
});

describe("launch paths of an app", () => {
  it("finds a launch path by id", () => {
    expect(launchPathOf(docs, "new")?.label).toBe("New docs");
    expect(launchPathOf(docs, "other")).toBeNull();
  });

  it("appends params as a query string", () => {
    expect(launchPathWithParams(launchPathRecord(), {})).toBe("/new");
    expect(launchPathWithParams(launchPathRecord(), { message: "hi there", account_id: "a/1" })).toBe(
      "/new?message=hi+there&account_id=a%2F1",
    );
  });
});

describe("the text path", () => {
  const launchPath = launchPathRecord({ path: "/new", params: ["message"], text_param: "message" });

  it("encodes the text into the text param, and runs the bare launch path for empty text", () => {
    expect(textPathOf(launchPath, "new chat")).toEqual({ kind: "path", path: "/new?message=new+chat" });
    expect(textPathOf(launchPath, "")).toEqual({ kind: "path", path: "/new" });
  });

  it("stands down on the encoded path's length, not the typed length", () => {
    const fitting = "x".repeat(MAX_WINDOW_PATH_LENGTH - "/new?message=".length);
    expect(textPathOf(launchPath, fitting).kind).toBe("path");
    expect(textPathOf(launchPath, `${fitting}x`)).toEqual({ kind: "disabled", reason: "Too long to send from here" });
    // Encoding triples a non-ASCII character, so a shorter text can be over the bound.
    expect(textPathOf(launchPath, "é".repeat(700)).kind).toBe("disabled");
  });

  it("stands down for a launch path with no text param", () => {
    expect(textPathOf(launchPathRecord(), "hi").kind).toBe("disabled");
  });
});

describe("the path a chat is shown at", () => {
  it("reads the chat's own page and its subagent views as showing it, and nothing else", () => {
    expect(chatPath("chat-7")).toBe("/chat-7");
    expect(isPathShowingChat("/chat-7", "chat-7")).toBe(true);
    expect(isPathShowingChat("/chat-7.agent-2.sess-3", "chat-7")).toBe(true);
    expect(isPathShowingChat("/chat-7?draft=hi", "chat-7")).toBe(true);
    // The chat list, another chat, and a chat whose id merely starts with this one are not it.
    expect(isPathShowingChat("/", "chat-7")).toBe(false);
    expect(isPathShowingChat("/chat-8", "chat-7")).toBe(false);
    expect(isPathShowingChat("/chat-70", "chat-7")).toBe(false);
  });
});
