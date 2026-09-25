/**
 * The element reference: the JSON description of one element on one page that a right-click
 * hands to a chat (docs/system/blueprint/element-reference-menu/, section 3.1). Built from the
 * DOM as it is -- the element's own attributes, a selector checked to match it alone -- with the
 * scope the shell's handshake gives the page around it, so an agent resolves it by grep and by
 * reading the page, never through a registry. Each reference carries a random ``reference_id``
 * (``REF-<11 base-36 characters>``): the name of the file it is attached to a message as, and
 * the word the message calls it by.
 *
 * Pure over the DOM: no framework, no message primitive. Bundled into the served
 * ``context_menu.js`` beside the menu, and imported from source by the built-in frontends.
 */

/** A rectangle in CSS pixels, as ``getBoundingClientRect`` reports it. */
export interface ReferenceBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** The scope a page knows itself by: the shell's handshake, or nothing on a top-level visit. */
export interface ReferenceScope {
  app: string | null;
  windowId: string | null;
  desktopId: string | null;
  clientId: string | null;
}

/** Where the right-click landed, in viewport and in document coordinates. */
export interface ReferenceClick {
  clientX: number;
  clientY: number;
  pageX: number;
  pageY: number;
}

export interface ElementReference {
  reference_id: string;
  app: string | null;
  window_id: string | null;
  desktop_id: string | null;
  client_id: string | null;
  page_origin: string;
  page_path: string;
  page_title: string;
  viewport: { width: number; height: number };
  pointer: { client_x: number; client_y: number; page_x: number; page_y: number };
  tag: string;
  id: string | null;
  classes: string[];
  attributes: Record<string, string>;
  role: string | null;
  aria_label: string | null;
  selection_text: string;
  selection_box: ReferenceBox | null;
  input_value: string | null;
  link_href: string | null;
  image_src: string | null;
  selector: string | null;
  bounding_box: ReferenceBox;
}

/** The one key a reference travels under. */
export const ELEMENT_REFERENCE_KEY = "element_reference";
/** What every reference id starts with; the rest is ``REFERENCE_ID_LENGTH`` base-36 characters. */
export const REFERENCE_ID_PREFIX = "REF-";
export const REFERENCE_ID_LENGTH = 11;
/** A whole reference id, as ``mintReferenceId`` makes one. */
export const REFERENCE_ID_PATTERN = new RegExp(`^${REFERENCE_ID_PREFIX}[0-9a-z]{${REFERENCE_ID_LENGTH}}$`);
const REFERENCE_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz";

/** A reference as it travels: the object under its one key. */
export interface ElementReferenceEnvelope {
  [ELEMENT_REFERENCE_KEY]: ElementReference;
}

const INPUT_VALUE_TAGS: ReadonlySet<string> = new Set(["input", "textarea", "select"]);
/** Input types that are buttons rather than fields: nothing to edit, no value worth reading. */
const BUTTON_INPUT_TYPES: ReadonlySet<string> = new Set([
  "button",
  "submit",
  "reset",
  "image",
  "checkbox",
  "radio",
  "file",
  "color",
  "range",
  "hidden",
]);

/** The element an event's target stands for: a text node's parent, the root for the document itself. */
export function elementOfTarget(target: EventTarget | null, ownerDocument: Document): Element {
  if (target instanceof Element) return target;
  if (target instanceof Node && target.parentElement !== null) return target.parentElement;
  return ownerDocument.documentElement;
}

/** Whether the element takes typed text: a text-like input, a textarea, or a contenteditable region. */
export function isEditableElement(element: Element): boolean {
  if (element instanceof HTMLTextAreaElement) return !element.disabled && !element.readOnly;
  if (element instanceof HTMLInputElement) {
    return !BUTTON_INPUT_TYPES.has(element.type) && !element.disabled && !element.readOnly;
  }
  return element instanceof HTMLElement && contentEditableRegionOf(element) !== null;
}

/** The ``contenteditable`` region the element sits in, or null outside one; read off the attributes
 *  (``isContentEditable`` is a rendering fact some environments never compute). */
export function contentEditableRegionOf(element: Element): Element | null {
  const region = element.closest("[contenteditable]");
  if (region === null) return null;
  const value = region.getAttribute("contenteditable");
  return value === "" || value === "true" || value === "plaintext-only" ? region : null;
}

/** The value of a field, or null for an element that has none. */
export function inputValueOf(element: Element): string | null {
  if (!INPUT_VALUE_TAGS.has(element.tagName.toLowerCase())) return null;
  return (element as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement).value;
}

/** The nearest enclosing anchor's absolute href, or null. */
export function linkHrefOf(element: Element): string | null {
  const anchor = element.closest("a[href]");
  if (!(anchor instanceof HTMLAnchorElement)) return null;
  return anchor.href;
}

/** An image's absolute src, or null for anything that is not an image. */
export function imageSrcOf(element: Element): string | null {
  if (!(element instanceof HTMLImageElement)) return null;
  return element.src === "" ? null : element.src;
}

function attributesOf(element: Element): Record<string, string> {
  const attributes: Record<string, string> = {};
  for (const attribute of Array.from(element.attributes)) {
    if (attribute.name === "class" || attribute.name === "id") continue;
    attributes[attribute.name] = attribute.value;
  }
  return attributes;
}

function idOf(element: Element): string | null {
  return element.id === "" ? null : element.id;
}

function classesOf(element: Element): string[] {
  return Array.from(element.classList);
}

function boxOf(rect: DOMRect): ReferenceBox {
  return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
}

function escapeIdentifier(value: string): string {
  return CSS.escape(value);
}

/** One level of the selector: the id when there is one, else the tag with its classes and an index among
 *  same-tag siblings when another sibling looks the same. */
function selectorStepOf(element: Element): { step: string; isAnchored: boolean } {
  const id = idOf(element);
  if (id !== null) return { step: `#${escapeIdentifier(id)}`, isAnchored: true };
  const tag = element.tagName.toLowerCase();
  const classes = classesOf(element);
  const base = classes.map((name) => `.${escapeIdentifier(name)}`).join("");
  const parent = element.parentElement;
  if (parent === null) return { step: `${tag}${base}`, isAnchored: false };
  const sameTagSiblings = Array.from(parent.children).filter((sibling) => sibling.tagName === element.tagName);
  const lookalike = sameTagSiblings.some(
    (sibling) => sibling !== element && classesOf(sibling).join(" ") === classes.join(" "),
  );
  if (!lookalike) return { step: `${tag}${base}`, isAnchored: false };
  const index = sameTagSiblings.indexOf(element) + 1;
  return { step: `${tag}${base}:nth-of-type(${index})`, isAnchored: false };
}

/**
 * A CSS selector matching exactly this element in its document, or null when the best try
 * matches more than one element or none (section 3.1.1): the check is what makes the field
 * trustworthy.
 */
export function uniqueSelectorFor(element: Element): string | null {
  const steps: string[] = [];
  let current: Element | null = element;
  while (current !== null && current !== element.ownerDocument.documentElement) {
    const { step, isAnchored } = selectorStepOf(current);
    steps.unshift(step);
    if (isAnchored) break;
    current = current.parentElement;
  }
  const selector = steps.join(" > ");
  if (selector === "") return null;
  let matches: NodeListOf<Element>;
  try {
    matches = element.ownerDocument.querySelectorAll(selector);
  } catch (error) {
    console.warn(`[element-reference] the selector built for the element is not valid: ${selector}`, error);
    return null;
  }
  return matches.length === 1 && matches[0] === element ? selector : null;
}

/** The document's selected text and the box of its range, read at the click. */
export function selectionOf(ownerDocument: Document): { text: string; box: ReferenceBox | null } {
  const selection = ownerDocument.getSelection();
  if (selection === null || selection.rangeCount === 0) return { text: "", box: null };
  const text = selection.toString();
  if (text === "") return { text: "", box: null };
  const range = selection.getRangeAt(0);
  // A range's box is a rendering fact; an environment without layout gives the selection no box.
  const box = typeof range.getBoundingClientRect === "function" ? boxOf(range.getBoundingClientRect()) : null;
  return { text, box };
}

/** A fresh reference id: the prefix and random base-36 characters, so two references never share a name and no
 *  counter has to be kept anywhere. */
export function mintReferenceId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(REFERENCE_ID_LENGTH));
  const characters = Array.from(bytes, (byte) => REFERENCE_ID_ALPHABET[byte % REFERENCE_ID_ALPHABET.length]);
  return `${REFERENCE_ID_PREFIX}${characters.join("")}`;
}

/** The name of the file a reference is attached to a message as. */
export function referenceFileNameOf(referenceId: string): string {
  return `${referenceId}.json`;
}

/** Build the reference of ``element`` for a right-click at ``click`` on a page with ``scope``. */
export function describeElement(element: Element, click: ReferenceClick, scope: ReferenceScope): ElementReference {
  const ownerDocument = element.ownerDocument;
  const view = ownerDocument.defaultView;
  const selection = selectionOf(ownerDocument);
  return {
    reference_id: mintReferenceId(),
    app: scope.app,
    window_id: scope.windowId,
    desktop_id: scope.desktopId,
    client_id: scope.clientId,
    page_origin: view?.location.origin ?? "",
    page_path: view === null ? "" : `${view.location.pathname}${view.location.search}`,
    page_title: ownerDocument.title,
    viewport: { width: view?.innerWidth ?? 0, height: view?.innerHeight ?? 0 },
    pointer: { client_x: click.clientX, client_y: click.clientY, page_x: click.pageX, page_y: click.pageY },
    tag: element.tagName.toLowerCase(),
    id: idOf(element),
    classes: classesOf(element),
    attributes: attributesOf(element),
    role: element.getAttribute("role"),
    aria_label: element.getAttribute("aria-label"),
    selection_text: selection.text,
    selection_box: selection.box,
    input_value: inputValueOf(element),
    link_href: linkHrefOf(element),
    image_src: imageSrcOf(element),
    selector: uniqueSelectorFor(element),
    bounding_box: boxOf(element.getBoundingClientRect()),
  };
}

/** What a page's last handshake from the shell says of it (``ShellHandshake`` without the path): the scope of
 *  every reference the page builds. ``app`` is optional so a handshake from a shell that sends none scopes one too. */
export interface ReferenceHandshake {
  app?: string;
  windowId: string;
  desktopId: string;
  clientId: string;
}

/** The scope a handshake gives a page, or the empty scope with none. */
export function scopeOfHandshake(handshake: ReferenceHandshake | null): ReferenceScope {
  if (handshake === null) return { app: null, windowId: null, desktopId: null, clientId: null };
  const nullIfEmpty = (value: string | undefined): string | null =>
    value === undefined || value === "" ? null : value;
  return {
    app: nullIfEmpty(handshake.app),
    windowId: nullIfEmpty(handshake.windowId),
    desktopId: nullIfEmpty(handshake.desktopId),
    clientId: nullIfEmpty(handshake.clientId),
  };
}

/** A fenced ``json`` block holding one object on one line: how a reference travels as text (a draft through the
 *  shell, the clipboard) until a chat attaches it as a file. */
export function jsonBlock(value: unknown): string {
  return "```json\n" + JSON.stringify(value) + "\n```";
}

/** The reference's block: the envelope in a fence. */
export function referenceBlock(reference: ElementReference): string {
  return jsonBlock({ [ELEMENT_REFERENCE_KEY]: reference });
}

/** The envelope as its file holds it: pretty-printed, with a final newline. */
export function referenceFileText(envelope: ElementReferenceEnvelope): string {
  return JSON.stringify(envelope, null, 2) + "\n";
}

/** One short line saying what a reference points at, for a chip or a tooltip: the element as a selector-like
 *  name (``button#save.primary``, the first three classes at most), then the app and the page it is on. */
export function referenceSummaryOf(reference: ElementReference): string {
  const id = reference.id === null ? "" : `#${reference.id}`;
  const classes = reference.classes
    .slice(0, 3)
    .map((name) => `.${name}`)
    .join("");
  const element = `${reference.tag}${id}${classes}`;
  const place = reference.app === null ? reference.page_path : `${reference.app} ${reference.page_path}`;
  return place === "" ? element : `${element} in ${place}`;
}

/** The envelope a block's JSON is, when it is one; null for any other block. */
export function referenceEnvelopeOf(json: string): ElementReferenceEnvelope | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(json);
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const keys = Object.keys(parsed);
  if (keys.length !== 1 || keys[0] !== ELEMENT_REFERENCE_KEY) return null;
  const inner = (parsed as Record<string, unknown>)[ELEMENT_REFERENCE_KEY];
  if (inner === null || typeof inner !== "object" || Array.isArray(inner)) return null;
  return parsed as ElementReferenceEnvelope;
}
