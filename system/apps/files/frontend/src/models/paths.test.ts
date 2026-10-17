import { describe, expect, it } from "vitest";

import {
  ancestorsOf,
  childPath,
  decodePathname,
  encodePath,
  extensionOf,
  invalidNameReason,
  isHiddenName,
  isWithin,
  nameOf,
  parentOf,
  stemLength,
  titleOf,
  uniqueName,
} from "./paths";

describe("paths", () => {
  it("walks up and names folders and files", () => {
    expect(parentOf("/a/b/")).toBe("/a/");
    expect(parentOf("/a/b.txt")).toBe("/a/");
    expect(parentOf("/")).toBeNull();
    expect(nameOf("/a/b/")).toBe("b");
    expect(childPath("/a/", "b", true)).toBe("/a/b/");
    expect(childPath("/a/", "b.txt", false)).toBe("/a/b.txt");
    expect(ancestorsOf("/a/b/")).toEqual(["/", "/a/", "/a/b/"]);
    expect(titleOf("/")).toBe("Files");
    expect(titleOf("/home/user/")).toBe("user");
  });

  it("tells what lies within a folder", () => {
    expect(isWithin("/a/b/c.txt", "/a/")).toBe(true);
    expect(isWithin("/a/", "/a/")).toBe(true);
    expect(isWithin("/ab/", "/a/")).toBe(false);
  });

  it("reads extensions, leaving a leading dot as part of the name", () => {
    expect(extensionOf("photo.JPG")).toBe("jpg");
    expect(extensionOf(".env")).toBe("");
    expect(extensionOf("Makefile")).toBe("");
    expect(stemLength("report.final.pdf")).toBe("report.final".length);
    expect(stemLength("folder")).toBe("folder".length);
  });

  it("hides dotted names, and search results with a dotted segment", () => {
    expect(isHiddenName(".git")).toBe(true);
    expect(isHiddenName("src/.cache/x")).toBe(true);
    expect(isHiddenName("notes.txt")).toBe(false);
  });

  it("refuses names a file cannot have", () => {
    expect(invalidNameReason("")).not.toBeNull();
    expect(invalidNameReason("a/b")).toBe('A name can\'t contain "/".');
    expect(invalidNameReason("..")).not.toBeNull();
    expect(invalidNameReason("fine.txt")).toBeNull();
  });

  it("numbers a taken name before its extension", () => {
    const taken = new Set(["up.txt", "up 2.txt"]);
    expect(uniqueName("up.txt", (name) => taken.has(name))).toBe("up 3.txt");
    expect(uniqueName("untitled folder", () => false)).toBe("untitled folder");
  });

  it("encodes each name and decodes back", () => {
    expect(encodePath("/my docs/a#1.txt")).toBe("/my%20docs/a%231.txt");
    expect(decodePathname("/my%20docs/a%231.txt")).toBe("/my docs/a#1.txt");
    expect(decodePathname("/bad%zz")).toBe("/bad%zz");
  });
});
