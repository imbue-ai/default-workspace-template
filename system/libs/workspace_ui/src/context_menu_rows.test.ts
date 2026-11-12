// @vitest-environment jsdom
/**
 * Which rows a right-click's target admits, and what the reference rows hand over: the standard
 * rows follow the target (editable, selected, a link, an image), the reference rows are always
 * there, and Explain and Modify grey without a shell to draft through. Also which right-clicks
 * belong to the desktop app's native menu.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { describeElement, referenceBlock } from "./element_reference";
import {
  NO_SHELL_DRAFT_REASON,
  draftTextOf,
  elementMenuRows,
  elementReferenceRows,
  explainPromptOf,
  isNativeMenuTarget,
  joinRowGroups,
  modifyPromptOf,
  standardContextMenuRows,
  targetOfEvent,
  type ContextMenuActionRow,
  type ContextMenuRow,
  type ContextMenuTarget,
} from "./context_menu_rows";

const CLICK = { clientX: 1, clientY: 2, pageX: 1, pageY: 2 };
const SCOPE = { app: "docs", windowId: "win-1", desktopId: "home", clientId: "client-1" };

function keysOf(rows: readonly ContextMenuRow[]): string[] {
  return rows.map((row) => (row.kind === "divider" ? "|" : row.key));
}

function targetOf(element: Element, selectionText = ""): ContextMenuTarget {
  return { element, selectionText, click: CLICK };
}

/** The action row keyed ``key``; a key no row carries is a failed test, not a silent miss. */
function actionRowOf(rows: readonly ContextMenuRow[], key: string): ContextMenuActionRow {
  const found = rows.find((row): row is ContextMenuActionRow => row.kind === "action" && row.key === key);
  if (found === undefined) throw new Error(`no row ${key} among ${keysOf(rows).join(", ")}`);
  return found;
}

function byId(id: string): Element {
  return document.getElementById(id) as Element;
}

let clipboard: {
  writeText: ReturnType<typeof vi.fn>;
  readText?: ReturnType<typeof vi.fn>;
  write?: ReturnType<typeof vi.fn>;
};
let openLink: ReturnType<typeof vi.fn<(anchor: HTMLAnchorElement) => void>>;

beforeEach(() => {
  openLink = vi.fn<(anchor: HTMLAnchorElement) => void>();
  clipboard = { writeText: vi.fn(async () => undefined), readText: vi.fn(async () => "pasted") };
  Object.defineProperty(navigator, "clipboard", { value: clipboard, configurable: true });
  document.body.innerHTML =
    '<p id="para">plain words</p>' +
    '<input id="field" value="hello world">' +
    '<input id="mail" type="email" value="a@b.example">' +
    '<input id="secret" type="password" value="hunter2">' +
    '<a id="link" href="/docs/intro">intro</a>' +
    '<img id="pic" src="/a.png">' +
    '<div id="note" contenteditable="true">editable</div>';
});

afterEach(() => {
  document.getSelection()?.removeAllRanges();
  document.body.innerHTML = "";
  delete (document as { execCommand?: unknown }).execCommand;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Stands in for the browser's ClipboardItem, which jsdom lacks: it keeps what it was built from. */
class FakeClipboardItem {
  constructor(readonly data: Record<string, Promise<Blob>>) {}
}

/** Give the browser an image clipboard, and ``image`` the pixels a loaded image has. */
function makeImageCopyable(image: Element): void {
  vi.stubGlobal("ClipboardItem", FakeClipboardItem);
  clipboard.write = vi.fn(async () => undefined);
  Object.defineProperties(image, {
    complete: { value: true, configurable: true },
    naturalWidth: { value: 30, configurable: true },
    naturalHeight: { value: 20, configurable: true },
  });
}

/** Make the document's selection the contents of ``element``. */
function selectContentsOf(element: Element): void {
  const range = document.createRange();
  range.selectNodeContents(element);
  const selection = document.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
}

describe("standardContextMenuRows", () => {
  it("offers nothing for a plain element with nothing selected", () => {
    expect(standardContextMenuRows(targetOf(byId("para")), openLink)).toEqual([]);
  });

  it("offers Copy alone for a selection on read-only text", () => {
    expect(keysOf(standardContextMenuRows(targetOf(byId("para"), "words"), openLink))).toEqual(["copy"]);
  });

  it("offers Paste and Select All for a field, and Cut and Copy once it has a selection", () => {
    const field = byId("field") as HTMLInputElement;
    expect(keysOf(standardContextMenuRows(targetOf(field), openLink))).toEqual(["paste", "select-all"]);
    field.setSelectionRange(0, 5);
    expect(keysOf(standardContextMenuRows(targetOf(field), openLink))).toEqual(["cut", "copy", "paste", "select-all"]);
  });

  it("offers Copy but not Cut on an editable target when the selection lies elsewhere on the page", () => {
    const field = byId("field") as HTMLInputElement;
    field.setSelectionRange(0, 0);
    expect(keysOf(standardContextMenuRows(targetOf(field, "plain words"), openLink))).toEqual([
      "copy",
      "paste",
      "select-all",
    ]);
    selectContentsOf(byId("para"));
    expect(keysOf(standardContextMenuRows(targetOf(byId("note"), "plain words"), openLink))).toEqual([
      "copy",
      "paste",
      "select-all",
    ]);
    selectContentsOf(byId("note"));
    expect(keysOf(standardContextMenuRows(targetOf(byId("note"), "editable"), openLink))).toEqual([
      "cut",
      "copy",
      "paste",
      "select-all",
    ]);
  });

  it("withholds Cut and Copy on a password field with a selection, keeping Paste and Select All", () => {
    const secret = byId("secret") as HTMLInputElement;
    secret.setSelectionRange(0, 6);
    expect(keysOf(standardContextMenuRows(targetOf(secret), openLink))).toEqual(["paste", "select-all"]);
  });

  it("withholds Paste where the browser withholds readText", () => {
    delete clipboard.readText;
    expect(keysOf(standardContextMenuRows(targetOf(byId("note")), openLink))).toEqual(["select-all"]);
  });

  it("offers the link rows in an anchor and the image row on an image, after the edit rows", () => {
    expect(keysOf(standardContextMenuRows(targetOf(byId("link"), "intro"), openLink))).toEqual([
      "copy",
      "|",
      "copy-link",
      "open-link",
    ]);
    expect(keysOf(standardContextMenuRows(targetOf(byId("pic")), openLink))).toEqual(["copy-image-address"]);
  });

  it("offers Copy image only for a loaded image whose pixels the page can read", () => {
    const picture = byId("pic") as HTMLImageElement;
    makeImageCopyable(picture);
    expect(keysOf(standardContextMenuRows(targetOf(picture), openLink))).toEqual(["copy-image", "copy-image-address"]);
    // Another origin's image, loaded without CORS, taints the canvas that would read it.
    picture.src = "https://images.example/a.png";
    expect(keysOf(standardContextMenuRows(targetOf(picture), openLink))).toEqual(["copy-image-address"]);
    picture.crossOrigin = "anonymous";
    expect(keysOf(standardContextMenuRows(targetOf(picture), openLink))).toEqual(["copy-image", "copy-image-address"]);
    picture.removeAttribute("crossorigin");
    picture.src = "data:image/png;base64,AAAA";
    expect(keysOf(standardContextMenuRows(targetOf(picture), openLink))).toEqual(["copy-image", "copy-image-address"]);
    delete clipboard.write;
    expect(keysOf(standardContextMenuRows(targetOf(picture), openLink))).toEqual(["copy-image-address"]);
  });

  it("copies an image as the PNG a canvas of its own size makes of it", async () => {
    const picture = byId("pic") as HTMLImageElement;
    makeImageCopyable(picture);
    const drawImage = vi.fn();
    const png = new Blob(["png"], { type: "image/png" });
    const drawnOn: HTMLCanvasElement[] = [];
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(function (this: HTMLCanvasElement) {
      drawnOn.push(this);
      return { drawImage } as unknown as CanvasRenderingContext2D;
    });
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback, type) => {
      expect(type).toBe("image/png");
      callback(png);
    });
    actionRowOf(standardContextMenuRows(targetOf(picture), openLink), "copy-image").onSelect();
    await vi.waitFor(() => expect(clipboard.write).toHaveBeenCalledTimes(1));
    expect(drawImage).toHaveBeenCalledWith(picture, 0, 0);
    expect([drawnOn[0].width, drawnOn[0].height]).toEqual([30, 20]);
    const [[item]] = clipboard.write!.mock.calls[0] as [[FakeClipboardItem]];
    expect(Object.keys(item.data)).toEqual(["image/png"]);
    expect(await item.data["image/png"]).toBe(png);
  });

  it("writes nothing, with a warning, when the canvas refuses to hand the image back", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const picture = byId("pic") as HTMLImageElement;
    makeImageCopyable(picture);
    // The browser's own ClipboardItem takes a promise without awaiting it; the write is what fails.
    clipboard.write = vi.fn(async ([item]: [FakeClipboardItem]) => void (await item.data["image/png"]));
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage: vi.fn(),
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(() => {
      throw new Error("tainted canvas");
    });
    actionRowOf(standardContextMenuRows(targetOf(picture), openLink), "copy-image").onSelect();
    await vi.waitFor(() => expect(warn).toHaveBeenCalledWith(expect.stringContaining("tainted canvas")));
  });

  it("copies a field's own selection, and cuts it out of the value", async () => {
    const field = byId("field") as HTMLInputElement;
    field.setSelectionRange(0, 5);
    const rows = standardContextMenuRows(targetOf(field), openLink);
    actionRowOf(rows, "copy").onSelect();
    await Promise.resolve();
    expect(clipboard.writeText).toHaveBeenCalledWith("hello");
    actionRowOf(rows, "cut").onSelect();
    await vi.waitFor(() => expect(field.value).toBe(" world"));
  });

  it("leaves a field's selection in place, with a warning, when the clipboard refuses the cut", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    clipboard.writeText = vi.fn(async () => {
      throw new Error("not focused");
    });
    const field = byId("field") as HTMLInputElement;
    field.setSelectionRange(0, 5);
    const rows = standardContextMenuRows(targetOf(field), openLink);
    actionRowOf(rows, "cut").onSelect();
    await vi.waitFor(() => expect(warn).toHaveBeenCalledWith(expect.stringContaining("not focused")));
    expect(field.value).toBe("hello world");
    warn.mockRestore();
  });

  it("pastes into and selects all of an input without a selection API through execCommand", async () => {
    const mail = byId("mail") as HTMLInputElement;
    const execCommand = vi.fn(() => true);
    Object.defineProperty(document, "execCommand", { value: execCommand, configurable: true });
    const rows = standardContextMenuRows(targetOf(mail), openLink);
    expect(keysOf(rows)).toEqual(["paste", "select-all"]);
    actionRowOf(rows, "paste").onSelect();
    await vi.waitFor(() => expect(execCommand).toHaveBeenCalledWith("insertText", false, "pasted"));
    expect(document.activeElement).toBe(mail);
    actionRowOf(rows, "select-all").onSelect();
    expect(execCommand).toHaveBeenCalledWith("selectAll");
  });

  it("focuses the contenteditable region, not the child clicked inside it, before an execCommand", async () => {
    const note = byId("note") as HTMLElement;
    note.innerHTML = '<span id="word">editable</span>';
    const execCommand = vi.fn(() => true);
    Object.defineProperty(document, "execCommand", { value: execCommand, configurable: true });
    const rows = standardContextMenuRows(targetOf(byId("word")), openLink);
    expect(keysOf(rows)).toEqual(["paste", "select-all"]);
    actionRowOf(rows, "paste").onSelect();
    await vi.waitFor(() => expect(execCommand).toHaveBeenCalledWith("insertText", false, "pasted"));
    expect(document.activeElement).toBe(note);
    (document.activeElement as HTMLElement).blur();
    actionRowOf(rows, "select-all").onSelect();
    expect(execCommand).toHaveBeenCalledWith("selectAll");
    expect(document.activeElement).toBe(note);
  });

  it("opens a link as a click on it would, handing over the link element under the target", () => {
    const link = byId("link");
    link.innerHTML = '<b id="inner">intro</b>';
    const rows = standardContextMenuRows(targetOf(byId("inner")), openLink);
    expect(actionRowOf(rows, "open-link").label).toBe("Open link");
    actionRowOf(rows, "open-link").onSelect();
    expect(openLink.mock.calls).toEqual([[link]]);
  });

  it("copies an absolute link address", async () => {
    const rows = standardContextMenuRows(targetOf(byId("link")), openLink);
    actionRowOf(rows, "copy-link").onSelect();
    await Promise.resolve();
    expect(clipboard.writeText).toHaveBeenCalledWith(`${window.location.origin}/docs/intro`);
  });
});

describe("elementReferenceRows", () => {
  it("copies the block, and drafts a prompt naming the reference over the block", () => {
    const reference = describeElement(byId("para"), CLICK, SCOPE);
    const draft = vi.fn();
    const rows = elementReferenceRows(reference, draft, true);
    expect(keysOf(rows)).toEqual(["copy-reference", "explain-element", "modify-element"]);
    expect(rows.map((row) => (row.kind === "divider" ? "|" : row.label))).toEqual([
      "Copy reference",
      "Explain...",
      "Modify...",
    ]);
    expect(actionRowOf(rows, "explain-element").isDisabled).toBe(false);
    actionRowOf(rows, "copy-reference").onSelect();
    expect(clipboard.writeText).toHaveBeenCalledWith(referenceBlock(reference));
    actionRowOf(rows, "explain-element").onSelect();
    actionRowOf(rows, "modify-element").onSelect();
    const id = reference.reference_id;
    expect(draft.mock.calls).toEqual([
      [draftTextOf(explainPromptOf(id), referenceBlock(reference))],
      [draftTextOf(modifyPromptOf(id), referenceBlock(reference))],
    ]);
    expect(draftTextOf(explainPromptOf("REF-abc"), "B")).toBe("Explain what I attached in REF-abc\n\nB");
    expect(modifyPromptOf("REF-abc")).toBe("Change REF-abc to ");
  });

  it("greys Explain and Modify with the reason when no draft can go", () => {
    const reference = describeElement(byId("para"), CLICK, SCOPE);
    const rows = elementReferenceRows(reference, vi.fn(), false);
    expect(actionRowOf(rows, "copy-reference").isDisabled).toBeUndefined();
    expect(actionRowOf(rows, "explain-element").isDisabled).toBe(true);
    expect(actionRowOf(rows, "explain-element").tooltip).toBe(NO_SHELL_DRAFT_REASON);
    expect(actionRowOf(rows, "modify-element").isDisabled).toBe(true);
  });
});

describe("elementMenuRows", () => {
  it("puts the page's own rows first, then the standard rows, then the reference rows, with dividers between", () => {
    const own: ContextMenuRow[] = [{ kind: "action", key: "rename", label: "Rename", onSelect: () => undefined }];
    const rows = elementMenuRows(targetOf(byId("link"), "intro"), SCOPE, vi.fn(), true, own, openLink);
    expect(keysOf(rows)).toEqual([
      "rename",
      "|",
      "copy",
      "|",
      "copy-link",
      "open-link",
      "|",
      "copy-reference",
      "explain-element",
      "modify-element",
    ]);
  });

  it("draws no divider before the reference rows when nothing else applies", () => {
    expect(keysOf(elementMenuRows(targetOf(byId("para")), SCOPE, vi.fn(), true, [], openLink))).toEqual([
      "copy-reference",
      "explain-element",
      "modify-element",
    ]);
  });
});

describe("isNativeMenuTarget", () => {
  afterEach(() => {
    delete (Range.prototype as { getClientRects?: unknown }).getClientRects;
  });

  /** Draw the document's selection as one box, where a layout (which jsdom lacks) would have put it. */
  function drawSelectionAt(box: { left: number; right: number; top: number; bottom: number }): void {
    Object.defineProperty(Range.prototype, "getClientRects", { value: () => [box], configurable: true });
  }

  it("takes text being edited, in a field or a contenteditable region", () => {
    expect(isNativeMenuTarget(targetOf(byId("field")), false)).toBe(true);
    expect(isNativeMenuTarget(targetOf(byId("note")), false)).toBe(true);
    // The page's own rows for the field do not keep it: the native menu is what corrects a spelling.
    expect(isNativeMenuTarget(targetOf(byId("field")), true)).toBe(true);
  });

  it("takes a click on the selection, and leaves one beside it to the page", () => {
    selectContentsOf(byId("para"));
    drawSelectionAt({ left: 0, right: 10, top: 0, bottom: 10 });
    expect(isNativeMenuTarget(targetOf(byId("para"), "plain words"), false)).toBe(true);
    expect(
      isNativeMenuTarget({ ...targetOf(byId("pic"), "plain words"), click: { ...CLICK, clientX: 50 } }, false),
    ).toBe(false);
  });

  it("leaves a selection it cannot place to the page", () => {
    selectContentsOf(byId("para"));
    expect(isNativeMenuTarget(targetOf(byId("para"), "plain words"), false)).toBe(false);
  });

  it("takes a plain link, but not one the page has rows for, nor an image inside one", () => {
    document.body.innerHTML += '<a id="thumb" href="/full.png"><img id="inner" src="/thumb.png"></a>';
    expect(isNativeMenuTarget(targetOf(byId("link")), false)).toBe(true);
    expect(isNativeMenuTarget(targetOf(byId("link")), true)).toBe(false);
    expect(isNativeMenuTarget(targetOf(byId("inner")), false)).toBe(false);
  });

  it("leaves an image and plain content to the page", () => {
    expect(isNativeMenuTarget(targetOf(byId("pic")), false)).toBe(false);
    expect(isNativeMenuTarget(targetOf(byId("para")), false)).toBe(false);
  });
});

describe("joinRowGroups and targetOfEvent", () => {
  it("drops empty groups and never doubles a divider", () => {
    const a: ContextMenuRow = { kind: "action", key: "a", label: "A", onSelect: () => undefined };
    expect(keysOf(joinRowGroups([[], [a], [], [a]]))).toEqual(["a", "|", "a"]);
  });

  it("reads the element, the selection, and the click off the event", () => {
    const paragraph = byId("para");
    const event = new MouseEvent("contextmenu", { clientX: 5, clientY: 6, bubbles: true });
    Object.defineProperty(event, "target", { value: paragraph.firstChild });
    const target = targetOfEvent(event, document);
    expect(target.element).toBe(paragraph);
    expect(target.selectionText).toBe("");
    expect(target.click.clientX).toBe(5);
    expect(target.click.clientY).toBe(6);
  });
});
