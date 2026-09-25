// @vitest-environment jsdom
/**
 * Which rows a right-click's target admits, and what the reference rows hand over: the standard
 * rows follow the target (editable, selected, a link, an image), the reference rows are always
 * there, and Explain and Modify grey without a shell to draft through.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { describeElement, referenceBlock } from "./element_reference";
import {
  EXPLAIN_PROMPT,
  MODIFY_PROMPT,
  NO_SHELL_DRAFT_REASON,
  draftTextOf,
  elementMenuRows,
  elementReferenceRows,
  joinRowGroups,
  standardContextMenuRows,
  targetOfEvent,
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

function byId(id: string): Element {
  return document.getElementById(id) as Element;
}

let clipboard: { writeText: ReturnType<typeof vi.fn>; readText?: ReturnType<typeof vi.fn> };

beforeEach(() => {
  clipboard = { writeText: vi.fn(async () => undefined), readText: vi.fn(async () => "pasted") };
  Object.defineProperty(navigator, "clipboard", { value: clipboard, configurable: true });
  document.body.innerHTML =
    '<p id="para">plain words</p>' +
    '<input id="field" value="hello world">' +
    '<input id="mail" type="email" value="a@b.example">' +
    '<a id="link" href="/docs/intro">intro</a>' +
    '<img id="pic" src="/a.png">' +
    '<div id="note" contenteditable="true">editable</div>';
});

afterEach(() => {
  document.body.innerHTML = "";
  delete (document as { execCommand?: unknown }).execCommand;
});

describe("standardContextMenuRows", () => {
  it("offers nothing for a plain element with nothing selected", () => {
    expect(standardContextMenuRows(targetOf(byId("para")))).toEqual([]);
  });

  it("offers Copy alone for a selection on read-only text", () => {
    expect(keysOf(standardContextMenuRows(targetOf(byId("para"), "words")))).toEqual(["copy"]);
  });

  it("offers Paste and Select All for a field, and Cut and Copy once it has a selection", () => {
    const field = byId("field") as HTMLInputElement;
    expect(keysOf(standardContextMenuRows(targetOf(field)))).toEqual(["paste", "select-all"]);
    field.setSelectionRange(0, 5);
    expect(keysOf(standardContextMenuRows(targetOf(field)))).toEqual(["cut", "copy", "paste", "select-all"]);
  });

  it("offers Copy but not Cut on a field when the selection lies elsewhere on the page", () => {
    const field = byId("field") as HTMLInputElement;
    field.setSelectionRange(0, 0);
    expect(keysOf(standardContextMenuRows(targetOf(field, "plain words")))).toEqual(["copy", "paste", "select-all"]);
    expect(keysOf(standardContextMenuRows(targetOf(byId("note"), "editable")))).toEqual([
      "cut",
      "copy",
      "paste",
      "select-all",
    ]);
  });

  it("withholds Paste where the browser withholds readText", () => {
    delete clipboard.readText;
    expect(keysOf(standardContextMenuRows(targetOf(byId("note"))))).toEqual(["select-all"]);
  });

  it("offers the link rows in an anchor and the image row on an image, after the edit rows", () => {
    expect(keysOf(standardContextMenuRows(targetOf(byId("link"), "intro")))).toEqual([
      "copy",
      "|",
      "copy-link",
      "open-link",
    ]);
    expect(keysOf(standardContextMenuRows(targetOf(byId("pic"))))).toEqual(["copy-image"]);
  });

  it("copies a field's own selection, and cuts it out of the value", async () => {
    const field = byId("field") as HTMLInputElement;
    field.setSelectionRange(0, 5);
    const rows = standardContextMenuRows(targetOf(field));
    const rowByKey = Object.fromEntries(rows.map((row) => [row.kind === "divider" ? "|" : row.key, row]));
    (rowByKey.copy as { onSelect: () => void }).onSelect();
    await Promise.resolve();
    expect(clipboard.writeText).toHaveBeenCalledWith("hello");
    (rowByKey.cut as { onSelect: () => void }).onSelect();
    await vi.waitFor(() => expect(field.value).toBe(" world"));
  });

  it("leaves a field's selection in place, with a warning, when the clipboard refuses the cut", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    clipboard.writeText = vi.fn(async () => {
      throw new Error("not focused");
    });
    const field = byId("field") as HTMLInputElement;
    field.setSelectionRange(0, 5);
    const rows = standardContextMenuRows(targetOf(field));
    (rows[0] as { key: string; onSelect: () => void }).onSelect();
    await vi.waitFor(() => expect(warn).toHaveBeenCalledWith(expect.stringContaining("not focused")));
    expect((rows[0] as { key: string }).key).toBe("cut");
    expect(field.value).toBe("hello world");
    warn.mockRestore();
  });

  it("pastes into and selects all of an input without a selection API through execCommand", async () => {
    const mail = byId("mail") as HTMLInputElement;
    const execCommand = vi.fn(() => true);
    Object.defineProperty(document, "execCommand", { value: execCommand, configurable: true });
    const rows = standardContextMenuRows(targetOf(mail));
    expect(keysOf(rows)).toEqual(["paste", "select-all"]);
    (rows[0] as { onSelect: () => void }).onSelect();
    await vi.waitFor(() => expect(execCommand).toHaveBeenCalledWith("insertText", false, "pasted"));
    expect(document.activeElement).toBe(mail);
    (rows[1] as { onSelect: () => void }).onSelect();
    expect(execCommand).toHaveBeenCalledWith("selectAll");
  });

  it("copies an absolute link address", async () => {
    const rows = standardContextMenuRows(targetOf(byId("link")));
    (rows[0] as { onSelect: () => void }).onSelect();
    await Promise.resolve();
    expect(clipboard.writeText).toHaveBeenCalledWith(`${window.location.origin}/docs/intro`);
  });
});

describe("elementReferenceRows", () => {
  it("copies the block, and drafts the prompt over the block with room to type", () => {
    const reference = describeElement(byId("para"), CLICK, SCOPE);
    const draft = vi.fn();
    const rows = elementReferenceRows(reference, draft, true);
    expect(keysOf(rows)).toEqual(["copy-element-path", "explain-element", "modify-element"]);
    const [copy, explain, modify] = rows as { onSelect: () => void; isDisabled?: boolean }[];
    expect(explain.isDisabled).toBe(false);
    copy.onSelect();
    expect(clipboard.writeText).toHaveBeenCalledWith(referenceBlock(reference));
    explain.onSelect();
    modify.onSelect();
    expect(draft.mock.calls).toEqual([
      [draftTextOf(EXPLAIN_PROMPT, referenceBlock(reference))],
      [draftTextOf(MODIFY_PROMPT, referenceBlock(reference))],
    ]);
    expect(draftTextOf(EXPLAIN_PROMPT, "B")).toBe("Explain this element:\n\nB\n\n");
  });

  it("greys Explain and Modify with the reason when no draft can go", () => {
    const reference = describeElement(byId("para"), CLICK, SCOPE);
    const rows = elementReferenceRows(reference, vi.fn(), false) as {
      key: string;
      isDisabled?: boolean;
      tooltip?: string;
    }[];
    expect(rows[0].isDisabled).toBeUndefined();
    expect(rows[1].isDisabled).toBe(true);
    expect(rows[1].tooltip).toBe(NO_SHELL_DRAFT_REASON);
    expect(rows[2].isDisabled).toBe(true);
  });
});

describe("elementMenuRows", () => {
  it("puts the page's own rows first, then the standard rows, then the reference rows, with dividers between", () => {
    const own: ContextMenuRow[] = [{ kind: "action", key: "rename", label: "Rename", onSelect: () => undefined }];
    const rows = elementMenuRows(targetOf(byId("link"), "intro"), SCOPE, vi.fn(), true, own);
    expect(keysOf(rows)).toEqual([
      "rename",
      "|",
      "copy",
      "|",
      "copy-link",
      "open-link",
      "|",
      "copy-element-path",
      "explain-element",
      "modify-element",
    ]);
  });

  it("draws no divider before the reference rows when nothing else applies", () => {
    expect(keysOf(elementMenuRows(targetOf(byId("para")), SCOPE, vi.fn(), true, []))).toEqual([
      "copy-element-path",
      "explain-element",
      "modify-element",
    ]);
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
