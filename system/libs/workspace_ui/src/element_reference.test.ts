// @vitest-environment jsdom
/**
 * The element reference, built from a real (jsdom) document: what it captures of an element,
 * its ancestors, and the page; the selector's uniqueness check; the block, its bound, and the
 * pointer form.
 */
import { afterEach, describe, expect, it } from "vitest";
import {
  ELEMENT_REFERENCE_BLOCK_LIMIT,
  ELEMENT_REFERENCE_FILE_KEY,
  ELEMENT_REFERENCE_KEY,
  ELEMENT_REFERENCE_SUMMARY_KEY,
  SUMMARY_TEXT_LIMIT,
  ancestorsOf,
  describeElement,
  elementOfTarget,
  isEditableElement,
  isOversizeBlock,
  pointerFormOf,
  referenceBlock,
  referenceEnvelopeOf,
  scopeOfHandshake,
  uniqueSelectorFor,
  type ElementReference,
} from "./element_reference";

const CLICK = { clientX: 10, clientY: 20, pageX: 10, pageY: 220 };
const SCOPE = { app: "chat", windowId: "win-1", desktopId: "home", clientId: "client-1" };

function render(html: string): void {
  document.body.innerHTML = html;
}

function byId(id: string): Element {
  const found = document.getElementById(id);
  if (found === null) throw new Error(`no #${id}`);
  return found;
}

afterEach(() => {
  document.body.innerHTML = "";
  document.title = "";
});

describe("describeElement", () => {
  it("captures the element, its ancestors, the page, and the scope", () => {
    document.title = "Plan the launch";
    render(
      '<div class="message-list"><div id="evt-1" class="message-user selected" data-chat-id="agent-1" style="color: red">' +
        '<a href="/docs/intro">Read  the\n intro</a></div></div>',
    );
    const link = document.querySelector("a") as Element;
    const reference = describeElement(link, CLICK, SCOPE);

    expect(reference.app).toBe("chat");
    expect(reference.window_id).toBe("win-1");
    expect(reference.desktop_id).toBe("home");
    expect(reference.client_id).toBe("client-1");
    expect(reference.page_title).toBe("Plan the launch");
    expect(reference.page_origin).toBe(window.location.origin);
    expect(reference.pointer).toEqual({ client_x: 10, client_y: 20, page_x: 10, page_y: 220 });
    expect(reference.tag).toBe("a");
    expect(reference.id).toBeNull();
    expect(reference.classes).toEqual([]);
    expect(reference.attributes).toEqual({ href: "/docs/intro" });
    expect(reference.text).toBe("Read the intro");
    expect(reference.link_href).toBe(`${window.location.origin}/docs/intro`);
    expect(reference.image_src).toBeNull();
    expect(reference.input_value).toBeNull();
    expect(reference.selector).toBe("#evt-1 > a");
    expect(reference.outer_html).toBe('<a href="/docs/intro">Read  the\n intro</a>');
    expect(reference.ancestors).toEqual([
      {
        tag: "div",
        id: "evt-1",
        classes: ["message-user", "selected"],
        attributes: { "data-chat-id": "agent-1", style: "color: red" },
      },
      { tag: "div", id: null, classes: ["message-list"], attributes: {} },
      { tag: "body", id: null, classes: [], attributes: {} },
    ]);
    expect(reference.selection_text).toBe("");
    expect(reference.selection_box).toBeNull();
    expect(Object.keys(reference.bounding_box)).toEqual(["x", "y", "width", "height"]);
  });

  it("reads a field's value, its editability, and an image's source", () => {
    render(
      '<input id="name" value="Ada"><textarea id="notes">hi</textarea><img id="pic" src="/a.png"><button id="go">Go</button>',
    );
    expect(describeElement(byId("name"), CLICK, SCOPE).input_value).toBe("Ada");
    expect(describeElement(byId("notes"), CLICK, SCOPE).input_value).toBe("hi");
    expect(describeElement(byId("pic"), CLICK, SCOPE).image_src).toBe(`${window.location.origin}/a.png`);
    expect(isEditableElement(byId("name"))).toBe(true);
    expect(isEditableElement(byId("notes"))).toBe(true);
    expect(isEditableElement(byId("go"))).toBe(false);
    expect(isEditableElement(byId("pic"))).toBe(false);
  });

  it("carries the selection and its box when text is selected", () => {
    render('<p id="para">Some words here</p>');
    const paragraph = byId("para");
    const range = document.createRange();
    range.selectNodeContents(paragraph);
    document.getSelection()?.addRange(range);
    const reference = describeElement(paragraph, CLICK, SCOPE);
    expect(reference.selection_text).toBe("Some words here");
    // jsdom lays nothing out, so the range has no box; a browser gives one.
    expect(reference.selection_box).toBeNull();
    document.getSelection()?.removeAllRanges();
  });

  it("has an empty scope on a top-level visit", () => {
    render('<p id="para">x</p>');
    const reference = describeElement(byId("para"), CLICK, scopeOfHandshake(null));
    expect(reference.app).toBeNull();
    expect(reference.window_id).toBeNull();
    expect(reference.client_id).toBeNull();
    expect(scopeOfHandshake({ clientId: "c", windowId: "", desktopId: "home" })).toEqual({
      app: null,
      windowId: null,
      desktopId: "home",
      clientId: "c",
    });
  });
});

describe("elementOfTarget", () => {
  it("answers the element, a text node's parent, and the root for anything else", () => {
    render('<p id="para">words</p>');
    const paragraph = byId("para");
    expect(elementOfTarget(paragraph, document)).toBe(paragraph);
    expect(elementOfTarget(paragraph.firstChild, document)).toBe(paragraph);
    expect(elementOfTarget(null, document)).toBe(document.documentElement);
    expect(elementOfTarget(document, document)).toBe(document.documentElement);
  });
});

describe("uniqueSelectorFor", () => {
  it("stops at an id and indexes a row among lookalike siblings", () => {
    render('<ul id="list"><li class="row">a</li><li class="row">b</li><li class="row other">c</li></ul>');
    const rows = document.querySelectorAll("li");
    expect(uniqueSelectorFor(rows[0])).toBe("#list > li.row:nth-of-type(1)");
    expect(uniqueSelectorFor(rows[1])).toBe("#list > li.row:nth-of-type(2)");
    // Its classes tell it from its siblings, so no index is needed.
    expect(uniqueSelectorFor(rows[2])).toBe("#list > li.row.other");
    for (const row of Array.from(rows)) {
      expect(document.querySelector(uniqueSelectorFor(row) as string)).toBe(row);
    }
  });

  it("walks up to the body when nothing on the way carries an id", () => {
    render('<div class="a"><div class="b"><span>x</span></div></div>');
    const span = document.querySelector("span") as Element;
    expect(uniqueSelectorFor(span)).toBe("body > div.a > div.b > span");
  });

  it("answers null when the best try matches more than one element, or none", () => {
    // Two identical subtrees under lookalike roots: the root gets an index, so the selector stays unique.
    render('<div class="a"><span>x</span></div><div class="a"><span>y</span></div>');
    const spans = document.querySelectorAll("span");
    expect(uniqueSelectorFor(spans[0])).toBe("body > div.a:nth-of-type(1) > span");
    // A duplicated id: the walk stops at it, and "#dup > span" matches both spans.
    render('<div id="dup"><span>x</span></div><div id="dup"><span>y</span></div>');
    const underDuplicateIds = document.querySelectorAll("span");
    expect(document.querySelectorAll("#dup > span")).toHaveLength(2);
    expect(uniqueSelectorFor(underDuplicateIds[0])).toBeNull();
    expect(uniqueSelectorFor(underDuplicateIds[1])).toBeNull();
    render("<p>x</p>");
    const detached = document.createElement("span");
    expect(uniqueSelectorFor(detached)).toBeNull();
  });

  it("escapes an id or class the selector syntax would misread", () => {
    render('<div id="a.b"><span class="x:y">t</span></div>');
    const span = document.querySelector("span") as Element;
    expect(uniqueSelectorFor(span)).toBe("#a\\.b > span.x\\:y");
    expect(document.querySelector(uniqueSelectorFor(span) as string)).toBe(span);
  });
});

describe("ancestorsOf", () => {
  it("ends at the body, and at the root outside one", () => {
    render('<div id="outer"><p>x</p></div>');
    const paragraph = document.querySelector("p") as Element;
    expect(ancestorsOf(paragraph).map((ancestor) => ancestor.tag)).toEqual(["div", "body"]);
    const detached = document.createElement("div");
    const inner = document.createElement("span");
    detached.appendChild(inner);
    expect(ancestorsOf(inner).map((ancestor) => ancestor.tag)).toEqual(["div"]);
  });
});

describe("the block and the pointer form", () => {
  function reference(text: string): ElementReference {
    render('<p id="para">x</p>');
    return { ...describeElement(byId("para"), CLICK, SCOPE), text };
  }

  it("fences the envelope on one line, and parses back to it", () => {
    const built = reference("x");
    const block = referenceBlock(built);
    expect(block.startsWith("```json\n{")).toBe(true);
    expect(block.endsWith("}\n```")).toBe(true);
    expect(block.split("\n")).toHaveLength(3);
    const json = block.split("\n")[1];
    expect(referenceEnvelopeOf(json)).toEqual({ [ELEMENT_REFERENCE_KEY]: built });
  });

  it("is oversize past the bound, fences included", () => {
    const small = referenceBlock(reference("x"));
    expect(isOversizeBlock(small)).toBe(false);
    expect(small.length).toBeLessThanOrEqual(ELEMENT_REFERENCE_BLOCK_LIMIT);
    const big = referenceBlock(reference("y".repeat(ELEMENT_REFERENCE_BLOCK_LIMIT)));
    expect(isOversizeBlock(big)).toBe(true);
  });

  it("summarises a reference behind its file, cutting only the text", () => {
    const built = reference("z".repeat(SUMMARY_TEXT_LIMIT + 50));
    const pointer = pointerFormOf(built, "/tmp/element_references/abc.json");
    expect(pointer[ELEMENT_REFERENCE_FILE_KEY]).toBe("/tmp/element_references/abc.json");
    expect(pointer[ELEMENT_REFERENCE_SUMMARY_KEY]).toEqual({
      app: "chat",
      window_id: "win-1",
      page_path: built.page_path,
      tag: "p",
      id: "para",
      selector: "#para",
      text: "z".repeat(SUMMARY_TEXT_LIMIT),
    });
  });

  it("recognises only an object under the one key as an envelope", () => {
    expect(referenceEnvelopeOf("not json")).toBeNull();
    expect(referenceEnvelopeOf('{"other": {}}')).toBeNull();
    expect(referenceEnvelopeOf(`{"${ELEMENT_REFERENCE_KEY}": {}, "extra": 1}`)).toBeNull();
    expect(referenceEnvelopeOf(`{"${ELEMENT_REFERENCE_KEY}": "text"}`)).toBeNull();
    expect(referenceEnvelopeOf(`{"${ELEMENT_REFERENCE_FILE_KEY}": "/tmp/x.json"}`)).toBeNull();
    expect(referenceEnvelopeOf(`{"${ELEMENT_REFERENCE_KEY}": {"tag": "p"}}`)).toEqual({
      [ELEMENT_REFERENCE_KEY]: { tag: "p" },
    });
  });
});
