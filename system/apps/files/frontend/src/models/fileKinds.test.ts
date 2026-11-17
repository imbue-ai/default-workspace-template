import { describe, expect, it } from "vitest";

import { badgeOf, familyOf, isMarkdown, isTextLike, kindLabelOf, previewKindOf } from "./fileKinds";

describe("file kinds", () => {
  it("names a kind for the Kind column", () => {
    expect(kindLabelOf("a.py", false)).toBe("Python script");
    expect(kindLabelOf("docs", true)).toBe("Folder");
    expect(kindLabelOf("weird.xyz", false)).toBe("XYZ file");
    expect(kindLabelOf("Makefile", false)).toBe("Document");
  });

  it("previews what the browser can draw, text when the server says it is text, else nothing", () => {
    expect(previewKindOf("a.png", false)).toBe("image");
    expect(previewKindOf("a.svg", true)).toBe("image");
    expect(previewKindOf("a.mp4", false)).toBe("video");
    expect(previewKindOf("a.pdf", false)).toBe("pdf");
    expect(previewKindOf("a.heic", false)).toBe("none");
    expect(previewKindOf("a.md", true)).toBe("text");
    expect(previewKindOf("a.zip", false)).toBe("none");
  });

  it("offers editing only on what reads as text", () => {
    expect(isTextLike("notes.md")).toBe(true);
    expect(isTextLike("uv.lock")).toBe(true);
    expect(isTextLike("Dockerfile")).toBe(true);
    expect(isTextLike("data.sqlite")).toBe(false);
    expect(isTextLike("photo.png")).toBe(false);
  });

  it("colours and tags by family", () => {
    expect(familyOf("a.csv", false)).toBe("spreadsheet");
    expect(familyOf("a", true)).toBe("folder");
    expect(badgeOf("archive.tar.gz")).toBe("GZ");
    expect(badgeOf("deck.pptx")).toBe("PPTX");
  });
});

describe("markdown", () => {
  it("is told by its extension, whatever its case", () => {
    expect(["plan.md", "README.MD", "notes.markdown", "a.mdown", "b.mkd"].map(isMarkdown)).toEqual([
      true,
      true,
      true,
      true,
      true,
    ]);
    expect(["notes.txt", "md", "plan.md.bak", "page.mdx"].map(isMarkdown)).toEqual([false, false, false, false]);
  });
});
